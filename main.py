"""AstrBot 插件：用 OpenList 的离线下载接口下载链接。

设计要点：
- 服务地址、认证方式、保存路径、离线下载方式全部由用户在 AstrBot 配置页手填，
  大模型只能决定"下哪些链接"，不能决定"下到哪里、用哪种方式"。
- 主入口是大模型的 function-calling 工具（自然语言），另保留 ``/ol`` 指令作兜底。
- 任务完成/失败后主动推送到提交任务的会话。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register

from .openlist_client import (
    DELETE_POLICIES,
    FINAL_TASK_STATES,
    OpenListAPIError,
    OpenListAuthError,
    OpenListClient,
    OpenListConfigError,
    OpenListError,
    describe_state,
)

PLUGIN_NAME = "astrbot_plugin_openlist_offline_download"

URL_PREFIXES = (
    "http://",
    "https://",
    "magnet:?",
    "ed2k://",
    "ftp://",
    "thunder://",
)

HELP_TEXT = """OpenList 离线下载

/ol dl <链接>    把链接交给 OpenList 离线下载（可一次多个，空格分隔）
/ol tasks        查看进行中的任务
/ol done         查看已结束的任务
/ol detail <id>  查看单个任务详情
/ol cancel <id>  取消任务
/ol retry <id>   重试任务
/ol clear        清空已结束的任务记录
/ol delete <id>  删除单个任务记录
/ol status       检查 OpenList 连接与配置

也可以直接跟我说：把 magnet:?xt=urn:btih:xxx 离线下载"""


def _human_size(num: Any) -> str:
    try:
        size = float(num or 0)
    except (TypeError, ValueError):
        return "-"
    if size <= 0:
        return "-"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.1f}{units[idx]}" if idx else f"{int(size)}B"


def _human_time(text: Any) -> str:
    """把 OpenList 的 RFC3339 时间转成本地时间字符串。"""
    if not text:
        return "-"
    raw = str(text)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        return raw[:19].replace("T", " ")


def _human_duration(task: dict[str, Any]) -> str:
    start, end = task.get("start_time"), task.get("end_time")
    if not start or not end:
        return "-"
    try:
        begin = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return "-"
    seconds = int((last - begin).total_seconds())
    if seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分{seconds % 60}秒"
    return f"{seconds // 3600}小时{seconds % 3600 // 60}分"


def _fmt_task_line(task: dict[str, Any]) -> str:
    state = describe_state(task.get("state"))
    progress = task.get("progress")
    try:
        pct = float(progress or 0)
    except (TypeError, ValueError):
        pct = 0.0
    bits = [f"{state} {pct:.0f}%"]
    size = _human_size(task.get("total_bytes"))
    if size != "-":
        bits.append(size)
    status = str(task.get("status") or "").strip()
    if status:
        bits.append(status)
    return f"· {task.get('name') or task.get('id')}\n  {' | '.join(bits)}\n  id: {task.get('id')}"


def _fmt_task_block(task: dict[str, Any]) -> str:
    lines = [
        f"任务：{task.get('name') or task.get('id')}",
        f"状态：{describe_state(task.get('state'))}"
        + (f"（{task.get('status')}）" if task.get("status") else ""),
        f"进度：{task.get('progress') or 0}%",
        f"大小：{_human_size(task.get('total_bytes'))}",
        f"开始：{_human_time(task.get('start_time'))}",
        f"结束：{_human_time(task.get('end_time'))}",
    ]
    if task.get("error"):
        lines.append(f"错误：{task.get('error')}")
    lines.append(f"id：{task.get('id')}")
    return "\n".join(lines)


@register(
    PLUGIN_NAME,
    "waittide",
    "通过 OpenList 接口离线下载 http/https/磁力/ed2k 链接，支持自然语言与完成推送",
    "1.0.0",
)
class OpenListOfflineDownload(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: Any = config or {}
        self._client: OpenListClient | None = None
        self._client_sig: tuple[Any, ...] | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._tracked: dict[str, dict[str, Any]] = {}
        self._state_file: str = ""
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle
    async def initialize(self) -> None:
        self._load_tracked()
        self._ensure_watcher()

    async def terminate(self) -> None:
        if self._watcher and not self._watcher.done():
            self._watcher.cancel()
            try:
                await self._watcher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._watcher = None
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ---------------------------------------------------------------- config
    def _cfg(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.config
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node if node is not None else default

    def _connection_cfg(self) -> dict[str, Any]:
        return {
            "base_url": str(self._cfg("connection.base_url", "") or "").strip(),
            "auth_mode": str(self._cfg("connection.auth_mode", "password") or "password"),
            "token": str(self._cfg("connection.access_token", "") or ""),
            "username": str(self._cfg("connection.username", "") or ""),
            "password": str(self._cfg("connection.password", "") or ""),
            "verify_tls": bool(self._cfg("connection.verify_tls", True)),
            "timeout": int(self._cfg("connection.timeout", 30) or 30),
        }

    def _download_path(self) -> str:
        raw = str(self._cfg("download.download_path", "/") or "/").strip()
        if not raw.startswith("/"):
            raw = "/" + raw
        return raw.rstrip("/") or "/"

    def _download_tool(self) -> str:
        return str(self._cfg("download.download_tool", "") or "").strip()

    def _delete_policy(self) -> str:
        policy = str(self._cfg("download.delete_policy", "") or "").strip()
        return policy if policy in DELETE_POLICIES else "delete_on_upload_succeed"

    def _max_urls(self) -> int:
        try:
            return max(1, int(self._cfg("download.max_urls", 10) or 10))
        except (TypeError, ValueError):
            return 10

    def _notify_enabled(self) -> bool:
        return bool(self._cfg("notify.enabled", True))

    def _notify_interval(self) -> int:
        try:
            return max(5, int(self._cfg("notify.interval", 30) or 30))
        except (TypeError, ValueError):
            return 30

    def _notify_expire(self) -> int:
        try:
            hours = max(1, int(self._cfg("notify.expire_hours", 24) or 24))
        except (TypeError, ValueError):
            hours = 24
        return hours * 3600

    def _admin_only(self) -> bool:
        return bool(self._cfg("advanced.admin_only", True))

    # ---------------------------------------------------------------- client
    async def _get_client(self) -> OpenListClient:
        cfg = self._connection_cfg()
        sig = tuple(cfg.values())
        if self._client is None or self._client_sig != sig:
            if self._client is not None:
                await self._client.close()
                self._client = None
            self._client = OpenListClient(
                cfg["base_url"],
                auth_mode=cfg["auth_mode"],
                token=cfg["token"],
                username=cfg["username"],
                password=cfg["password"],
                timeout=cfg["timeout"],
                verify_tls=cfg["verify_tls"],
            )
            self._client_sig = sig
        self._ensure_watcher()
        return self._client

    async def _client_or_hint(self) -> tuple[OpenListClient | None, str]:
        try:
            return await self._get_client(), ""
        except OpenListConfigError as exc:
            return None, (
                f"{exc}。请在 AstrBot 管理面板 → 插件 → OpenList 离线下载 → 配置 中填写"
                "「OpenList 服务地址」「认证方式」以及账号/密钥，并设置「离线下载保存路径」。"
            )
        except OpenListError as exc:
            return None, str(exc)

    def _perm_denied(self, event: AstrMessageEvent) -> bool:
        if not self._admin_only():
            return False
        try:
            return not event.is_admin()
        except Exception:  # noqa: BLE001
            return False

    # -------------------------------------------------------------- tracking
    def _data_dir(self) -> str:
        try:
            path = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # noqa: BLE001
            path = os.path.join("data", "plugin_data", PLUGIN_NAME)
        os.makedirs(path, exist_ok=True)
        return path

    def _load_tracked(self) -> None:
        try:
            self._state_file = os.path.join(self._data_dir(), "tracked.json")
            if os.path.isfile(self._state_file):
                with open(self._state_file, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self._tracked = {
                        str(k): v for k, v in data.items() if isinstance(v, dict)
                    }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[openlist] 读取任务跟踪状态失败：{exc}")

    def _save_tracked(self) -> None:
        if not self._state_file:
            return
        try:
            tmp = f"{self._state_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._tracked, fh, ensure_ascii=False)
            os.replace(tmp, self._state_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[openlist] 保存任务跟踪状态失败：{exc}")

    def _track(self, tasks: list[dict[str, Any]], session: str) -> None:
        if not self._notify_enabled() or not session:
            return
        now = time.time()
        for task in tasks:
            task_id = task.get("id")
            if not task_id:
                continue
            self._tracked[str(task_id)] = {
                "session": session,
                "name": task.get("name") or str(task_id),
                "created_at": now,
                "last_state": task.get("state"),
            }
        self._save_tracked()

    def _ensure_watcher(self) -> None:
        if not self._notify_enabled():
            return
        if self._watcher and not self._watcher.done():
            return
        try:
            self._watcher = asyncio.create_task(self._watch_loop())
        except RuntimeError:
            self._watcher = None

    async def _watch_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                if self._tracked:
                    await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[openlist] 任务轮询失败：{exc}")
            await asyncio.sleep(self._notify_interval())

    async def _poll_once(self) -> None:
        client, hint = await self._client_or_hint()
        if client is None:
            logger.debug(f"[openlist] 轮询跳过：{hint}")
            return
        pending = await client.list_tasks("undone")
        done = await client.list_tasks("done")
        pending_map = {str(t.get("id")): t for t in pending if t.get("id")}
        done_map = {str(t.get("id")): t for t in done if t.get("id")}

        async with self._lock:
            now = time.time()
            finished: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
            for task_id, meta in list(self._tracked.items()):
                if task_id in pending_map:
                    meta["last_state"] = pending_map[task_id].get("state")
                    continue
                if task_id in done_map:
                    task = done_map[task_id]
                    try:
                        state = int(task.get("state"))
                    except (TypeError, ValueError):
                        state = -1
                    if state in FINAL_TASK_STATES:
                        finished.append((task_id, task, meta))
                        continue
                if now - float(meta.get("created_at") or 0) > self._notify_expire():
                    self._tracked.pop(task_id, None)

            for task_id, task, meta in finished:
                self._tracked.pop(task_id, None)
                await self._push_finished(task, meta)
            if finished:
                self._save_tracked()

    async def _push_finished(self, task: dict[str, Any], meta: dict[str, Any]) -> None:
        session = str(meta.get("session") or "")
        if not session:
            return
        try:
            state = int(task.get("state"))
        except (TypeError, ValueError):
            state = -1
        name = task.get("name") or meta.get("name") or task.get("id")
        if state == 2:
            text = (
                f"✅ OpenList 离线下载完成\n"
                f"任务：{name}\n"
                f"大小：{_human_size(task.get('total_bytes'))}\n"
                f"耗时：{_human_duration(task)}\n"
                f"保存路径：{self._download_path()}"
            )
        else:
            reason = str(task.get("error") or task.get("status") or "").strip()
            text = (
                f"❌ OpenList 离线下载未成功（{describe_state(state)}）\n"
                f"任务：{name}\n"
                + (f"原因：{reason}\n" if reason else "")
                + f"id：{task.get('id')}"
            )
        try:
            ok = await self.context.send_message(
                session, MessageChain(chain=[Plain(text=text)])
            )
            if not ok:
                logger.warning(f"[openlist] 推送失败（会话无可用平台）：{session}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[openlist] 推送消息异常：{exc}")

    # --------------------------------------------------------------- actions
    def _split_urls(self, text: str) -> list[str]:
        urls: list[str] = []
        for token in str(text or "").replace("\n", " ").split():
            token = token.strip().strip("<>\"'")
            if token.lower().startswith(URL_PREFIXES):
                urls.append(token)
        return urls

    async def _submit(
        self, urls: list[str], event: AstrMessageEvent | None
    ) -> str:
        client, hint = await self._client_or_hint()
        if client is None:
            return hint
        if not urls:
            return "没有识别到可下载的链接（支持 http/https、磁力链、ed2k）。"
        limit = self._max_urls()
        skipped = 0
        if len(urls) > limit:
            skipped = len(urls) - limit
            urls = urls[:limit]

        path = self._download_path()
        tool = self._download_tool()
        if not tool:
            try:
                available = await client.offline_download_tools(path)
            except OpenListError as exc:
                return f"读取离线下载方式失败：{exc}"
            if not available:
                return (
                    f"路径 {path} 下没有可用的离线下载方式。"
                    "请检查 OpenList 里该路径的存储是否已启用离线下载。"
                )
            tool = available[0]

        try:
            tasks = await client.add_offline_download(
                urls, path, tool, self._delete_policy()
            )
        except OpenListAuthError as exc:
            return f"OpenList 认证失败：{exc}"
        except OpenListAPIError as exc:
            return f"提交离线下载失败：{exc}"
        except OpenListError as exc:
            return f"提交离线下载失败：{exc}"

        if event is not None:
            self._track(tasks, event.unified_msg_origin)

        lines = [f"已提交 {len(urls)} 个链接到 {path}（方式：{tool}）"]
        if skipped:
            lines.append(f"超出单次上限，另有 {skipped} 个链接未提交。")
        if tasks:
            for task in tasks:
                lines.append(_fmt_task_line(task))
            lines.append("完成后我会主动通知你。")
        else:
            lines.append("服务端未返回任务信息，请用 /ol tasks 查看进度。")
        return "\n".join(lines)

    # ------------------------------------------------------------ LLM tools
    @filter.llm_tool(name="openlist_offline_download")
    async def tool_add_offline_download(
        self, event: AstrMessageEvent, urls: list
    ) -> str:
        """把链接提交给 OpenList 做离线下载（http/https 直链、BT 磁力链、ed2k 等）。

        用户给出一个或多个下载链接、并要求"离线下载/下载/保存/推到网盘"时使用。
        保存路径和离线下载方式已由插件配置固定，无需也不能指定。

        Args:
            urls(list[string]): 要离线下载的链接列表，支持 http/https 直链、magnet:?xt=urn:btih: 磁力链、ed2k:// 链接
        """
        if self._perm_denied(event):
            return "只有管理员可以提交离线下载任务。"
        cleaned: list[str] = []
        for item in urls or []:
            cleaned.extend(self._split_urls(str(item)))
        return await self._submit(cleaned, event)

    @filter.llm_tool(name="openlist_tasks")
    async def tool_list_tasks(self, event: AstrMessageEvent, scope: str = "undone") -> str:
        """查看 OpenList 离线下载任务列表与进度。

        用户问"下好了吗""进度如何""有哪些任务""下载记录"时使用。

        Args:
            scope(string): undone 表示进行中的任务，done 表示已结束的任务，all 表示全部
        """
        client, hint = await self._client_or_hint()
        if client is None:
            return hint
        scope = (scope or "undone").strip().lower()
        try:
            if scope == "all":
                pending = await client.list_tasks("undone")
                done = await client.list_tasks("done")
                tasks = pending + done
            elif scope == "done":
                tasks = await client.list_tasks("done")
            else:
                scope = "undone"
                tasks = await client.list_tasks("undone")
        except OpenListError as exc:
            return f"读取任务列表失败：{exc}"
        if not tasks:
            return {
                "undone": "当前没有进行中的离线下载任务。",
                "done": "当前没有已结束的离线下载任务。",
                "all": "当前没有任何离线下载任务。",
            }[scope]
        label = {"undone": "进行中", "done": "已结束", "all": "全部"}[scope]
        body = "\n".join(_fmt_task_line(task) for task in tasks[:20])
        more = f"\n（仅显示前 20 条，共 {len(tasks)} 条）" if len(tasks) > 20 else ""
        return f"离线下载任务（{label}）：\n{body}{more}"

    @filter.llm_tool(name="openlist_task_detail")
    async def tool_task_detail(self, event: AstrMessageEvent, task_id: str) -> str:
        """查看单个离线下载任务的详细信息，包括失败原因。

        用户询问某个任务为什么失败、具体进度或详情时使用。

        Args:
            task_id(string): 任务 id，可从 openlist_tasks 的返回中获取
        """
        client, hint = await self._client_or_hint()
        if client is None:
            return hint
        task_id = str(task_id or "").strip()
        if not task_id:
            return "需要提供任务 id。"
        try:
            task = await client.task_info(task_id)
        except OpenListError as exc:
            return f"读取任务详情失败：{exc}"
        if not task:
            return f"没有找到任务 {task_id}。"
        return _fmt_task_block(task)

    @filter.llm_tool(name="openlist_task_action")
    async def tool_task_action(
        self, event: AstrMessageEvent, action: str, task_id: str = ""
    ) -> str:
        """对离线下载任务执行操作：取消、重试、删除或清空已结束任务。

        用户说"取消那个下载""重新下载""清理下载记录"时使用。

        Args:
            action(string): cancel 取消任务，retry 重试任务，delete 删除任务记录，clear_done 清空所有已结束的任务
            task_id(string): 任务 id；clear_done 时可不填
        """
        if self._perm_denied(event):
            return "只有管理员可以操作离线下载任务。"
        client, hint = await self._client_or_hint()
        if client is None:
            return hint
        action = (action or "").strip().lower()
        task_id = str(task_id or "").strip()
        if action not in ("cancel", "retry", "delete", "clear_done"):
            return "action 只能是 cancel / retry / delete / clear_done。"
        if action != "clear_done" and not task_id:
            return f"{action} 操作需要提供任务 id。"
        try:
            await client.task_action(action, task_id)
        except OpenListError as exc:
            return f"操作失败：{exc}"
        if task_id:
            self._tracked.pop(task_id, None)
            self._save_tracked()
        return {
            "cancel": f"已取消任务 {task_id}。",
            "retry": f"已重新提交任务 {task_id}。",
            "delete": f"已删除任务记录 {task_id}。",
            "clear_done": "已清空所有已结束的离线下载任务。",
        }[action]

    @filter.llm_tool(name="openlist_status")
    async def tool_status(self, event: AstrMessageEvent) -> str:
        """检查 OpenList 插件配置与连接状态：服务地址、认证是否有效、下载路径与离线下载方式。

        用户问"OpenList 连上了吗""配置对不对""为什么用不了"时使用。
        """
        client, hint = await self._client_or_hint()
        if client is None:
            return hint
        try:
            settings = await client.ping()
            me = await client.me()
        except OpenListAuthError as exc:
            return f"OpenList 认证失败：{exc}"
        except OpenListError as exc:
            return f"OpenList 连接失败：{exc}"
        path = self._download_path()
        configured = self._download_tool() or "（自动选择）"
        try:
            tools = await client.offline_download_tools(path)
            tools_text = "、".join(tools) if tools else "无（该路径不支持离线下载）"
        except OpenListError as exc:
            tools_text = f"读取失败：{exc}"
        return "\n".join(
            [
                "OpenList 状态正常",
                f"服务地址：{client.base_url}",
                f"版本：{settings.get('version') or settings.get('title') or '未知'}",
                f"认证方式：{client.auth_mode}",
                f"当前账号：{me.get('username', '未知')}（角色 {me.get('role', '-')}）",
                f"下载路径：{path}",
                f"配置的下载方式：{configured}",
                f"该路径可用方式：{tools_text}",
                f"完成推送：{'开启' if self._notify_enabled() else '关闭'}",
            ]
        )

    # -------------------------------------------------------------- commands
    @filter.command("ol", alias={"openlist"})
    async def ol(self, event: AstrMessageEvent):
        """OpenList 离线下载（指令兜底）。用法见 /ol help"""
        text = (event.get_message_str() or "").strip()
        for prefix in ("/openlist", "/ol", "openlist", "ol"):
            if text.lower().startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        parts = text.split()
        sub = parts[0].lower() if parts else "help"
        rest = " ".join(parts[1:]).strip()

        if sub in ("help", "", "-h", "--help"):
            return event.plain_result(HELP_TEXT)

        if sub == "status":
            return event.plain_result(
                await self.tool_status(event)  # type: ignore[arg-type]
            )

        if sub in ("dl", "download", "add"):
            if self._perm_denied(event):
                return event.plain_result("只有管理员可以提交离线下载任务。")
            urls = self._split_urls(rest)
            return event.plain_result(await self._submit(urls, event))

        if sub in ("tasks", "task", "ls"):
            return event.plain_result(
                await self.tool_list_tasks(event, "undone")  # type: ignore[arg-type]
            )

        if sub in ("done", "finished"):
            return event.plain_result(
                await self.tool_list_tasks(event, "done")  # type: ignore[arg-type]
            )

        if sub in ("detail", "info"):
            return event.plain_result(
                await self.tool_task_detail(event, rest)  # type: ignore[arg-type]
            )

        if sub in ("cancel", "retry", "delete", "clear"):
            if self._perm_denied(event):
                return event.plain_result("只有管理员可以操作离线下载任务。")
            action = "clear_done" if sub == "clear" else sub
            return event.plain_result(
                await self.tool_task_action(event, action, rest)  # type: ignore[arg-type]
            )

        return event.plain_result(f"未知子指令：{sub}\n\n{HELP_TEXT}")
