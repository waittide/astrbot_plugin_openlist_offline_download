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
import posixpath
import shlex
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
    TASK_KINDS,
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

HELP_TEXT = """OpenList 离线下载与文件管理

【离线下载】
/ol dl <链接>      提交离线下载（可一次多个，空格分隔）
/ol tasks           查看进行中的任务
/ol done            查看已结束的任务
/ol detail <id>     查看单个任务详情
/ol cancel <id>     取消任务
/ol retry <id>      重试任务
/ol clear           清空已结束的任务记录
/ol delete <id>     删除单个任务记录

【文件管理】
/ol ls [路径]                     列出目录（默认根目录）
/ol mkdir <路径>                  新建目录
/ol mv <源路径...> <目标目录>     移动
/ol cp <源路径...> <目标目录>     复制
/ol rm <路径...>                  删除（目录会连内容一起删）
/ol rn <路径> <新名字>            重命名

/ol status          检查 OpenList 连接与配置

路径带空格请用引号括起来，例如：
/ol cp "/115/临时/我的 文件.mkv" /115/整理

也可以直接跟我说：把 magnet:?xt=urn:btih:xxx 离线下载；
看看 /115/临时 里有什么；把 a.mkv 改名为 b.mkv"""


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


def _norm_path(raw: Any) -> str:
    """把用户给的路径规范成以 / 开头的绝对路径。"""
    text = str(raw or "").strip().strip("`'\"<>")
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    normalized = posixpath.normpath(text)
    return normalized if normalized.startswith("/") else "/" + normalized


def _fmt_entry(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "")
    stamp = _human_time(item.get("modified"))
    if item.get("is_dir"):
        return f"📁 {name}/  {stamp}"
    return f"📄 {name}  {_human_size(item.get('size'))}  {stamp}"


def _split_path(raw: Any) -> tuple[str, str]:
    """把路径拆成 (所在目录, 名字)。"""
    path = _norm_path(raw)
    if path in ("", "/"):
        return "/", ""
    return posixpath.dirname(path) or "/", posixpath.basename(path)


def _is_storage_root(path: str) -> bool:
    """一级目录在 OpenList 里就是存储挂载点，不允许重命名/删除/移动。"""
    return path != "/" and path.count("/") == 1


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

    def _confirm_destructive(self) -> bool:
        return bool(self._cfg("advanced.confirm_destructive", True))

    def _needs_confirm(self, dangerous: bool, confirm: Any) -> bool:
        return bool(dangerous) and self._confirm_destructive() and not bool(confirm)

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

    def _track(
        self,
        tasks: list[dict[str, Any]],
        session: str,
        kind: str = "offline_download",
    ) -> None:
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
                "kind": kind,
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
        kinds = {
            str(meta.get("kind") or "offline_download")
            for meta in self._tracked.values()
        }
        pending_map: dict[tuple[str, str], dict[str, Any]] = {}
        done_map: dict[tuple[str, str], dict[str, Any]] = {}
        for kind in kinds:
            for task in await client.list_tasks("undone", kind):
                if task.get("id"):
                    pending_map[(kind, str(task["id"]))] = task
            for task in await client.list_tasks("done", kind):
                if task.get("id"):
                    done_map[(kind, str(task["id"]))] = task

        async with self._lock:
            now = time.time()
            finished: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
            for task_id, meta in list(self._tracked.items()):
                key = (str(meta.get("kind") or "offline_download"), task_id)
                if key in pending_map:
                    meta["last_state"] = pending_map[key].get("state")
                    continue
                if key in done_map:
                    task = done_map[key]
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
        kind = str(meta.get("kind") or "offline_download")
        label = TASK_KINDS.get(kind, "任务")
        if state == 2:
            text = (
                f"✅ OpenList {label}完成\n"
                f"任务：{name}\n"
                f"大小：{_human_size(task.get('total_bytes'))}\n"
                f"耗时：{_human_duration(task)}"
            )
            if kind == "offline_download":
                text += f"\n保存路径：{self._download_path()}"
        else:
            reason = str(task.get("error") or task.get("status") or "").strip()
            text = (
                f"❌ OpenList {label}未成功（{describe_state(state)}）\n"
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

    # -------------------------------------------------------------- 文件管理
    async def _list_files(self, path: str) -> str:
        client, hint = await self._client_or_hint()
        if client is None:
            return hint
        target = _norm_path(path or "/")
        try:
            info = await client.stat(target) if target != "/" else None
        except OpenListError as exc:
            return f"列出目录失败：{exc}"
        if info is not None and not info.get("is_dir"):
            lines = [
                f"路径：{target}",
                f"大小：{_human_size(info.get('size'))}",
                f"修改时间：{_human_time(info.get('modified'))}",
            ]
            if info.get("sign") or info.get("raw_url"):
                lines.append(f"直链：{info.get('sign') or info.get('raw_url')}")
            return "\n".join(lines)
        try:
            data = await client.list_dir(target, per_page=200, refresh=True)
        except OpenListAPIError as exc:
            # 无「刷新目录」权限的账号会 403，退回普通列表。
            if exc.code == 403 or "Refresh" in str(exc):
                try:
                    data = await client.list_dir(target, per_page=200)
                except OpenListError as inner:
                    return f"列出目录失败：{inner}"
            else:
                return f"列出目录失败：{exc}"
        except OpenListError as exc:
            return f"列出目录失败：{exc}"
        content = data.get("content") or []
        if not content:
            return f"目录 {target} 是空的。"
        total = data.get("total") or len(content)
        header = f"目录：{target}（共 {total} 项"
        if data.get("write") is not None:
            header += "，可写" if data.get("write") else "，只读"
        header += "）"
        lines = [header]
        for item in content[:50]:
            lines.append(_fmt_entry(item))
        if total > 50:
            lines.append(f"……仅显示前 50 项，共 {total} 项")
        return "\n".join(lines)

    def _preview(
        self, header: str, items: list[str], tip: str = ""
    ) -> str:
        lines = [header]
        lines.extend(f"· {item}" for item in items[:30])
        if len(items) > 30:
            lines.append(f"……等共 {len(items)} 项")
        if tip:
            lines.append(tip)
        lines.append(
            "确认无误后回复我，我再执行；也可以直接用 /ol 指令执行。"
            "（如需关闭二次确认：插件配置 → 高级 → 文件操作二次确认）"
        )
        return "\n".join(lines)

    async def _check_target_dir(self, client: OpenListClient, dst: str) -> str:
        """确认目标目录存在且确实是目录，返回错误文本或空串。"""
        if dst == "/":
            return ""
        try:
            info = await client.stat(dst)
        except OpenListError as exc:
            return f"检查目标目录失败：{exc}"
        if info is None:
            return f"目标目录不存在：{dst}\n可以先新建：/ol mkdir {dst}"
        if not info.get("is_dir"):
            return f"目标不是目录：{dst}。如果要改名请用重命名。"
        return ""

    async def _transfer(
        self,
        kind: str,
        paths: list[str],
        dst_dir: str,
        overwrite: Any = False,
        confirm: Any = False,
        event: AstrMessageEvent | None = None,
    ) -> str:
        """移动 / 复制。kind 为 move 或 copy。"""
        label = "移动" if kind == "move" else "复制"
        sources = [_norm_path(p) for p in (paths or []) if str(p or "").strip()]
        if not sources:
            return f"需要提供要{label}的文件或目录路径。"
        if len(sources) > 50:
            return f"单次最多{label} 50 项。"
        for src in sources:
            if src == "/":
                return "不能操作根目录。"
            if _is_storage_root(src):
                return f"{src} 是存储挂载点，不能{label}。"
        dst = _norm_path(dst_dir)
        if not dst:
            return f"需要提供目标目录。"

        client, hint = await self._client_or_hint()
        if client is None:
            return hint
        err = await self._check_target_dir(client, dst)
        if err:
            return err

        if self._needs_confirm(kind == "move" or bool(overwrite), confirm):
            return self._preview(
                f"将要{label} {len(sources)} 项到 {dst}：",
                sources,
                "（移动后原位置的文件会消失，这是不可撤销操作）" if kind == "move" else "",
            )

        # 按所在目录分组，OpenList 的 move/copy 接口以“源目录 + 名字列表”为单位。
        groups: dict[str, list[str]] = {}
        for src in sources:
            parent, name = _split_path(src)
            if not name:
                return f"无法解析路径：{src}"
            if parent == dst:
                return f"{src} 已经在 {dst} 里了。"
            groups.setdefault(parent, []).append(name)

        # 预检同名冲突，给出能看懂的提示（服务端只会回 file [x] exists）。
        conflicts: list[str] = []
        if not overwrite:
            for src in sources:
                _, name = _split_path(src)
                target = posixpath.join(dst, name) if dst != "/" else "/" + name
                try:
                    if await client.stat(target) is not None:
                        conflicts.append(target)
                except OpenListError:
                    continue
            if conflicts:
                return (
                    f"目标目录里已经有同名的 {len(conflicts)} 项：\n"
                    + "\n".join(f"· {c}" for c in conflicts[:20])
                    + "\n\n不会自动覆盖。如需覆盖请明确说明，我再执行（overwrite=True）。"
                )

        done: list[str] = []
        tasks: list[dict[str, Any]] = []
        for parent, names in groups.items():
            try:
                data = await client.move(
                    parent, dst, names, overwrite=bool(overwrite)
                ) if kind == "move" else await client.copy(
                    parent, dst, names, overwrite=bool(overwrite)
                )
            except OpenListAuthError:
                raise
            except OpenListError as exc:
                return f"{label}失败：{exc}"
            created = data.get("tasks") if isinstance(data, dict) else None
            if isinstance(created, list):
                tasks.extend(t for t in created if isinstance(t, dict))
            else:
                done.extend(names)

        if event is not None and tasks:
            self._track(tasks, event.unified_msg_origin, kind)

        lines: list[str] = []
        if done:
            lines.append(f"已{label} {len(done)} 项到 {dst}。")
        if tasks:
            lines.append(
                f"已创建 {len(tasks)} 个{label}任务（跨存储或大文件会后台执行）："
            )
            for task in tasks:
                lines.append(_fmt_task_line(task))
            lines.append("完成后我会主动通知你。")
        if not lines:
            lines.append(f"{label}请求已提交（服务端未返回详情）。")
        return "\n".join(lines)

    async def _rename(
        self,
        path: str,
        new_name: str,
        overwrite: Any = False,
        confirm: Any = False,
    ) -> str:
        src = _norm_path(path)
        name = str(new_name or "").strip().strip("/")
        if not src or src == "/":
            return "需要提供要重命名的完整路径。"
        if not name:
            return "需要提供新名字。"
        if "/" in name or "\\" in name:
            return "新名字里不能包含路径分隔符；要换目录请用移动。"
        if _is_storage_root(src):
            return f"{src} 是存储挂载点，不能重命名。"
        parent, old_name = _split_path(src)
        if not old_name:
            return f"无法解析路径：{src}"
        if old_name == name and not overwrite:
            return "新名字和原名相同。"

        client, hint = await self._client_or_hint()
        if client is None:
            return hint
        target = posixpath.join(parent, name) if parent != "/" else "/" + name
        try:
            exists = await client.stat(target)
        except OpenListError as exc:
            return f"检查目标失败：{exc}"
        if exists is not None and not overwrite:
            return f"{target} 已存在。如需覆盖，请明确说明并允许覆盖。"

        if self._needs_confirm(bool(overwrite), confirm):
            return self._preview(
                "将要重命名（会覆盖同名文件）：",
                [f"{src}  →  {target}"],
            )
        try:
            await client.rename(src, name, overwrite=bool(overwrite))
        except OpenListError as exc:
            return f"重命名失败：{exc}"
        return f"已重命名：{src} → {target}"

    async def _remove(self, paths: list[str], confirm: Any = False) -> str:
        targets = [_norm_path(p) for p in (paths or []) if str(p or "").strip()]
        if not targets:
            return "需要提供要删除的路径。"
        if len(targets) > 50:
            return "单次最多删除 50 项。"
        for target in targets:
            if target == "/":
                return "不能删除根目录。"
            if _is_storage_root(target):
                return f"{target} 是存储挂载点，不能删除。"

        client, hint = await self._client_or_hint()
        if client is None:
            return hint

        # 先确认存在，并统计目录内容，方便用户判断影响面。
        detail: list[str] = []
        for target in targets:
            try:
                info = await client.stat(target)
            except OpenListError as exc:
                return f"检查 {target} 失败：{exc}"
            if info is None:
                return f"不存在：{target}"
            if info.get("is_dir"):
                try:
                    sub = await client.list_dir(target, per_page=200, refresh=True)
                except OpenListError:
                    sub = {"content": [], "total": 0}
                count = sub.get("total") or len(sub.get("content") or [])
                detail.append(f"{target}/（目录，内含 {count} 项，会一并删除）")
            else:
                detail.append(f"{target}（{_human_size(info.get('size'))}）")

        if self._needs_confirm(True, confirm):
            return self._preview(
                f"将要删除 {len(targets)} 项（不可恢复）：", detail
            )

        groups: dict[str, list[str]] = {}
        for target in targets:
            parent, name = _split_path(target)
            groups.setdefault(parent, []).append(name)
        for parent, names in groups.items():
            try:
                await client.remove(parent, names)
            except OpenListError as exc:
                return f"删除失败（{parent}）：{exc}"
        return "已删除：\n" + "\n".join(f"· {item}" for item in detail)

    @filter.llm_tool(name="openlist_file_ops")
    async def tool_file_ops(
        self,
        event: AstrMessageEvent,
        action: str,
        paths: list | None = None,
        dst_dir: str = "",
        new_name: str = "",
        overwrite: bool = False,
        confirm: bool = False,
    ) -> str:
        """OpenList 网盘（远程存储）的文件管理：列出/移动/复制/删除/重命名/新建目录。

        只要用户说的是 OpenList / 网盘 / 网盘挂载里的路径（例如 /115/...、/teldrive/...、
        /infinicloud/...、/strm/...），想看目录里有什么、把文件挪到别的目录、复制一份、
        删掉文件、改名字、新建文件夹，都必须用这个工具，不要用 shell / 本地文件工具——
        那些路径在服务器本地并不存在。
        删除与移动默认先返回待确认清单（不执行），你应当把清单转述给用户，
        得到用户明确同意后再带 confirm=True 重新调用一次。

        Args:
            action(string): list 列表，move 移动，copy 复制，remove 删除，rename 重命名，mkdir 新建目录
            paths(list[string]): 要操作的路径列表；action=list 时可传多个目录；action=rename 时只需一个路径
            dst_dir(string): 目标目录（move/copy 必填；rename 可填新名字所在目录，一般留空）
            new_name(string): 新名字（仅 action=rename 时需要，不能包含路径分隔符）
            overwrite(bool): 是否覆盖同名文件，默认 false
            confirm(bool): 用户已确认为危险操作时传 true，默认 false
        """
        action = (action or "").strip().lower()
        items = [str(p) for p in (paths or []) if str(p or "").strip()]
        if action in ("move", "copy", "remove", "rename", "mkdir"):
            if self._perm_denied(event):
                return "只有管理员可以修改 OpenList 上的文件。"
        if action in ("list", "ls", ""):
            targets = items or ["/"]
            if len(targets) == 1:
                return await self._list_files(targets[0])
            chunks = []
            for target in targets[:5]:
                chunks.append(await self._list_files(target))
            if len(targets) > 5:
                chunks.append(f"……仅列出前 5 个路径，共 {len(targets)} 个")
            return "\n\n".join(chunks)
        if action == "mkdir":
            if not items:
                return "需要提供要新建的目录路径。"
            client, hint = await self._client_or_hint()
            if client is None:
                return hint
            target = _norm_path(items[0])
            try:
                await client.mkdir(target)
            except OpenListError as exc:
                return f"新建目录失败：{exc}"
            return f"已新建目录：{target}"
        if action in ("move", "mv"):
            return await self._transfer(
                "move", items, dst_dir, overwrite, confirm, event
            )
        if action in ("copy", "cp"):
            return await self._transfer(
                "copy", items, dst_dir, overwrite, confirm, event
            )
        if action == "remove":
            return await self._remove(items, confirm)
        if action == "rename":
            if not items:
                return "需要提供要重命名的路径。"
            src = items[0]
            name = (new_name or "").strip()
            if not name and dst_dir:
                # 允许模型把新名字写进 dst_dir，容错一点。
                name = dst_dir.strip().strip("/")
            return await self._rename(src, name, overwrite, confirm)
        return "action 只能是 list / move / copy / remove / rename / mkdir。"

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

        if sub in ("tasks", "task"):
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

        if sub in ("ls", "dir", "list"):
            target = ""
            if rest:
                try:
                    target = shlex.split(rest)[0]
                except ValueError:
                    target = rest
            return event.plain_result(await self._list_files(target))

        if sub in ("mkdir", "md"):
            if self._perm_denied(event):
                return event.plain_result("只有管理员可以修改 OpenList 上的文件。")
            return event.plain_result(
                await self.tool_file_ops(  # type: ignore[arg-type]
                    event, "mkdir", [rest]
                )
            )

        if sub in ("mv", "move", "cp", "copy"):
            if self._perm_denied(event):
                return event.plain_result("只有管理员可以修改 OpenList 上的文件。")
            try:
                args = shlex.split(rest)
            except ValueError:
                args = rest.split()
            if len(args) < 2:
                return event.plain_result(
                    f"用法：/ol {sub} <源路径...> <目标目录>"
                )
            return event.plain_result(
                await self._transfer(
                    "move" if sub in ("mv", "move") else "copy",
                    args[:-1],
                    args[-1],
                    False,
                    True,
                    event,
                )
            )

        if sub in ("rm", "del", "remove"):
            if self._perm_denied(event):
                return event.plain_result("只有管理员可以修改 OpenList 上的文件。")
            try:
                args = shlex.split(rest)
            except ValueError:
                args = rest.split()
            return event.plain_result(await self._remove(args, True))

        if sub in ("rn", "rename"):
            if self._perm_denied(event):
                return event.plain_result("只有管理员可以修改 OpenList 上的文件。")
            try:
                args = shlex.split(rest)
            except ValueError:
                args = rest.split()
            if len(args) < 2:
                return event.plain_result("用法：/ol rn <路径> <新名字>")
            return event.plain_result(await self._rename(args[0], " ".join(args[1:])))

        if sub in ("cancel", "retry", "delete", "clear"):
            if self._perm_denied(event):
                return event.plain_result("只有管理员可以操作离线下载任务。")
            action = "clear_done" if sub == "clear" else sub
            return event.plain_result(
                await self.tool_task_action(event, action, rest)  # type: ignore[arg-type]
            )

        return event.plain_result(f"未知子指令：{sub}\n\n{HELP_TEXT}")
