"""OpenList HTTP 客户端（离线下载与任务相关接口）。

只依赖 aiohttp（AstrBot 自带），认证支持两种方式：
- ``token``：访问密钥，直接放进 ``Authorization`` 请求头（裸值，不带 Bearer 前缀）。
- ``password``：用户名 + 密码，先 ``POST /api/auth/login`` 换取 JWT，再放入同一个请求头；
  JWT 会缓存，遇到 401 自动重新登录一次。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import aiohttp

from astrbot.api import logger

# OpenList 任务状态码（server/handles/task.go）
TASK_STATE_TEXT: dict[int, str] = {
    0: "等待中",
    1: "进行中",
    2: "已完成",
    3: "取消中",
    4: "已取消",
    5: "出错",
    6: "失败中",
    7: "失败",
    8: "等待重试",
    9: "重试前",
}

# 终态：已完成 / 已取消 / 出错 / 失败
FINAL_TASK_STATES: frozenset[int] = frozenset({2, 4, 5, 7})

DELETE_POLICIES = (
    "delete_on_upload_succeed",
    "delete_on_upload_failed",
    "delete_never",
    "delete_always",
    "upload_download_stream",
)

# OpenList 内置的离线下载工具名（internal/offline_download/*，即 IsReady() 为 true 的那些）。
# 顺序影响 /ol status 里的展示，不影响提交。
OFFLINE_TOOL_NAMES: tuple[str, ...] = (
    "115 Open",
    "115 Cloud",
    "123 Open",
    "123Pan",
    "PikPak",
    "Thunder",
    "ThunderX",
    "ThunderBrowser",
    "GuangYaPan",
    "aria2",
    "qBittorrent",
    "Transmission",
)

# 本插件不支持的离线下载方式：
# SimpleHttp 是把 http(s) 链接下载到 OpenList 本机临时目录再上传，速度与稳定性都取决于
# OpenList 服务器自身，而且只认 http(s) 直链，所以插件里一律不提供、也不会自动选到它。
UNSUPPORTED_OFFLINE_TOOLS: frozenset[str] = frozenset({"SimpleHttp"})

# 「自动选择」在下拉框里的取值（写入配置后按空值处理）。
OFFLINE_TOOL_AUTO = "auto"


def filter_offline_tools(names: Any) -> list[str]:
    """过滤掉插件不支持的离线下载方式，保持服务端返回的顺序。"""
    if not isinstance(names, (list, tuple)):
        return []
    return [
        str(item)
        for item in names
        if str(item).strip() and str(item) not in UNSUPPORTED_OFFLINE_TOOLS
    ]


# 需要跟踪/查询的任务类别（server/handles/task.go SetupTaskRoute）
TASK_KINDS = {
    "offline_download": "离线下载",
    "move": "移动",
    "copy": "复制",
    "upload": "上传",
    "decompress": "解压",
}


class OpenListError(Exception):
    """OpenList 调用异常基类。"""


class OpenListConfigError(OpenListError):
    """插件配置不完整或不合法。"""


class OpenListAuthError(OpenListError):
    """认证失败（密钥无效、账号密码错误等）。"""


class OpenListAPIError(OpenListError):
    """服务端返回了非 200 的业务码。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def describe_state(state: Any) -> str:
    """把任务状态码翻译成中文。"""
    try:
        code = int(state)
    except (TypeError, ValueError):
        return str(state)
    return TASK_STATE_TEXT.get(code, f"未知状态({code})")


def parse_sign_expire(sign: str) -> int | None:
    """从 OpenList 的 sign 里取出过期时间戳。

    sign 的格式是 ``base64url(hmac):expire``（pkg/sign/hmac.go），``expire`` 为 0
    表示长期有效。拿不到就是 None（未知）。
    """
    text = str(sign or "").strip().rstrip("/")
    if not text or ":" not in text:
        return None
    try:
        return int(text.rsplit(":", 1)[1])
    except ValueError:
        return None


def encode_path(path: str) -> str:
    """按 OpenList 的方式对路径分片做 URL 编码（拼接 /d 直链用）。"""
    segments = str(path or "/").split("/")
    return "/".join(quote(seg, safe="") for seg in segments)


class OpenListClient:
    """极简异步 OpenList 客户端，只覆盖本插件需要的接口。"""

    def __init__(
        self,
        base_url: str,
        *,
        auth_mode: str = "password",
        token: str = "",
        username: str = "",
        password: str = "",
        timeout: int = 30,
        verify_tls: bool = True,
    ) -> None:
        base = (base_url or "").strip().rstrip("/")
        if base.endswith("/api"):
            base = base[: -len("/api")]
        if not base:
            raise OpenListConfigError("未配置 OpenList 服务地址（base_url）")
        if not base.lower().startswith(("http://", "https://")):
            base = "http://" + base
        self.base_url = base
        self.auth_mode = (auth_mode or "password").strip().lower()
        if self.auth_mode not in ("token", "password"):
            raise OpenListConfigError(f"未知的认证方式：{auth_mode}")
        self.access_token = (token or "").strip()
        self.username = (username or "").strip()
        self.password = password or ""
        self.timeout = max(5, int(timeout or 30))
        self.verify_tls = bool(verify_tls)
        self._session: aiohttp.ClientSession | None = None
        self._jwt: str = ""
        self._jwt_expire_at: float = 0.0

    # ---------------------------------------------------------------- session
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=None if self.verify_tls else False)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------- auth
    async def _login(self) -> str:
        if not self.username or not self.password:
            raise OpenListConfigError("认证方式为用户名密码时，必须填写用户名和密码")
        data = await self._raw_request(
            "POST",
            "/auth/login",
            json_body={"username": self.username, "password": self.password},
            with_auth=False,
        )
        token = (data or {}).get("token") if isinstance(data, dict) else None
        if not token:
            raise OpenListAuthError("登录成功但响应中没有 token")
        self._jwt = str(token)
        # OpenList 的 JWT 默认 48 小时有效，这里保守按 6 小时缓存。
        self._jwt_expire_at = time.time() + 6 * 3600
        logger.debug("[openlist] 用户名密码登录成功")
        return self._jwt

    async def _auth_token(self) -> str:
        if self.auth_mode == "token":
            if not self.access_token:
                raise OpenListConfigError("认证方式为访问密钥时，必须填写访问密钥")
            return self.access_token
        if self._jwt and time.time() < self._jwt_expire_at:
            return self._jwt
        return await self._login()

    # --------------------------------------------------------------- requests
    async def _raw_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        with_auth: bool = True,
    ) -> Any:
        session = await self._get_session()
        headers = {"Accept": "application/json"}
        if with_auth:
            headers["Authorization"] = await self._auth_token()
        url = f"{self.base_url}/api{path}"
        try:
            async with session.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=headers,
            ) as resp:
                text = await resp.text()
                try:
                    body = await _loads(text)
                except ValueError:
                    if resp.status in (401, 403):
                        raise OpenListAuthError(
                            f"OpenList 认证失败（HTTP {resp.status}），"
                            "请检查访问密钥或用户名密码",
                        ) from None
                    raise OpenListAPIError(
                        f"OpenList 返回了非 JSON 响应（HTTP {resp.status}）：{text[:200]}",
                    ) from None
                if not isinstance(body, dict):
                    body = {}
                code = body.get("code")
                # 真正的认证失败：OpenList 既可能回 HTTP 401，也可能回 HTTP 200
                # 但业务码 401（token is invalidated）。403 是权限/文件已存在等
                # 业务拒绝，不能当成认证失败去重登。
                if resp.status == 401 or code == 401:
                    raise OpenListAuthError(
                        str(body.get("message")
                            or f"OpenList 认证失败（HTTP {resp.status}）"
                            )
                        + "（请检查访问密钥或用户名密码）"
                    )
                if resp.status >= 400:
                    raise OpenListAPIError(
                        f"OpenList 请求失败（HTTP {resp.status}）："
                        f"{body.get('message') or text[:200]}",
                        code if isinstance(code, int) else None,
                    )
                if code not in (None, 200):
                    raise OpenListAPIError(
                        str(body.get("message") or "未知错误"),
                        code if isinstance(code, int) else None,
                    )
                return body.get("data")
        except aiohttp.ClientError as exc:
            raise OpenListError(f"无法连接 OpenList（{self.base_url}）：{exc}") from exc
        except TimeoutError:
            raise OpenListError(f"连接 OpenList 超时（{self.base_url}）") from None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """带自动重登的请求。"""
        try:
            return await self._raw_request(
                method, path, json_body=json_body, params=params
            )
        except OpenListAuthError:
            if self.auth_mode != "password":
                raise
            # JWT 可能已过期，强制重新登录后重试一次。
            self._jwt = ""
            self._jwt_expire_at = 0.0
            return await self._raw_request(
                method, path, json_body=json_body, params=params
            )

    # ------------------------------------------------------------- 业务接口
    async def ping(self) -> dict[str, Any]:
        """读取 /api/public/settings，用于连通性检查。"""
        session = await self._get_session()
        url = f"{self.base_url}/api/public/settings"
        try:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise OpenListError(f"OpenList 无法访问（HTTP {resp.status}）")
                body = await _loads(await resp.text())
                if body.get("code") not in (None, 200):
                    raise OpenListAPIError(str(body.get("message") or "未知错误"))
                return body.get("data") or {}
        except aiohttp.ClientError as exc:
            raise OpenListError(f"无法连接 OpenList（{self.base_url}）：{exc}") from exc
        except TimeoutError:
            raise OpenListError(f"连接 OpenList 超时（{self.base_url}）") from None

    async def me(self) -> dict[str, Any]:
        return await self.request("GET", "/me") or {}

    async def list_dir(
        self, path: str = "/", page: int = 1, per_page: int = 100, refresh: bool = False
    ) -> dict[str, Any]:
        """列出目录内容，返回 {content, total, write, provider}。"""
        path = (path or "/").strip() or "/"
        data = await self.request(
            "POST",
            "/fs/list",
            json_body={
                "path": path,
                "page": max(1, int(page or 1)),
                "per_page": max(1, int(per_page or 100)),
                "refresh": bool(refresh),
                "password": "",
            },
        )
        if not isinstance(data, dict):
            return {"content": [], "total": 0}
        content = data.get("content")
        data["content"] = (
            [item for item in content if isinstance(item, dict)]
            if isinstance(content, list)
            else []
        )
        return data

    async def list_dirs(self, path: str = "/") -> list[dict[str, Any]]:
        """只列出子目录（/fs/dirs），返回 [{name, modified}]。"""
        data = await self.request(
            "GET", "/fs/dirs", params={"path": (path or "/").strip() or "/"}
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def stat(self, path: str) -> dict[str, Any] | None:
        """查询单个路径的信息；不存在时返回 None。"""
        try:
            data = await self.request(
                "GET", "/fs/get", params={"path": (path or "/").strip() or "/"}
            )
        except OpenListAPIError as exc:
            message = str(exc).lower()
            if "not found" in message or "不存在" in message:
                return None
            raise
        return data if isinstance(data, dict) else None

    async def file_link(self, path: str) -> dict[str, Any] | None:
        """取单个文件的下载直链与有效期。

        返回在 ``/fs/get`` 结果上补充了这些字段的字典：
        - ``url``：可直接访问的下载地址（有签名时用 ``/d<path>?sign=...``，否则用服务端 raw_url）
        - ``expire_ts``：链接过期时间戳（0 表示长期有效，None 表示未知）
        - ``source``：``sign`` 签名直链，``raw`` 存储驱动给的原始直链，``unsigned`` 无签名 /d 直链
        目录不会返回链接（OpenList 没有目录直链），``url`` 为空串。
        """
        info = await self.stat(path)
        if info is None:
            return None
        result = dict(info)
        result["path"] = path
        if info.get("is_dir"):
            result["url"] = ""
            result["expire_ts"] = None
            result["source"] = "dir"
            return result
        sign = str(info.get("sign") or "").strip()
        raw_url = str(info.get("raw_url") or "").strip()
        if sign:
            result["url"] = f"{self.base_url}/d{encode_path(path)}?sign={sign}"
            result["expire_ts"] = parse_sign_expire(sign)
            result["source"] = "sign"
        elif raw_url:
            # 没有签名时服务端给的 raw_url 本身就是可下载地址（由网盘签发）。
            result["url"] = raw_url
            result["expire_ts"] = None
            result["source"] = "raw"
        else:
            result["url"] = f"{self.base_url}/d{encode_path(path)}"
            result["expire_ts"] = None
            result["source"] = "unsigned"
        return result

    async def list_dir_all(
        self,
        path: str = "/",
        *,
        per_page: int = 200,
        max_pages: int = 5,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """翻页列出目录下的全部条目（最多 max_pages 页，避免巨目录拖垮性能）。"""
        items: list[dict[str, Any]] = []
        for page in range(1, max(1, int(max_pages or 1)) + 1):
            data = await self.list_dir(
                path,
                page=page,
                per_page=max(1, int(per_page or 200)),
                refresh=bool(refresh) and page == 1,
            )
            content = data.get("content") or []
            items.extend(content)
            total = data.get("total")
            try:
                total_num = int(total) if total is not None else len(items)
            except (TypeError, ValueError):
                total_num = len(items)
            if len(content) < per_page or len(items) >= total_num:
                break
        return items

    async def mkdir(self, path: str) -> None:
        await self.request("POST", "/fs/mkdir", json_body={"path": path})

    async def rename(
        self, path: str, name: str, *, overwrite: bool = False
    ) -> None:
        """重命名：path 是旧完整路径，name 是新名字（不能带路径分隔符）。"""
        await self.request(
            "POST",
            "/fs/rename",
            json_body={"path": path, "name": name, "overwrite": bool(overwrite)},
        )

    async def move(
        self,
        src_dir: str,
        dst_dir: str,
        names: list[str],
        *,
        overwrite: bool = False,
        skip_existing: bool = False,
        merge: bool = False,
    ) -> dict[str, Any]:
        """批量移动。names 是 src_dir 下的名字；跨存储时返回异步任务。"""
        data = await self.request(
            "POST",
            "/fs/move",
            json_body={
                "src_dir": src_dir,
                "dst_dir": dst_dir,
                "names": names,
                "overwrite": bool(overwrite),
                "skip_existing": bool(skip_existing),
                "merge": bool(merge),
            },
        )
        return data if isinstance(data, dict) else {}

    async def copy(
        self,
        src_dir: str,
        dst_dir: str,
        names: list[str],
        *,
        overwrite: bool = False,
        skip_existing: bool = False,
        merge: bool = False,
    ) -> dict[str, Any]:
        """批量复制。names 是 src_dir 下的名字；跨存储时返回异步任务。"""
        data = await self.request(
            "POST",
            "/fs/copy",
            json_body={
                "src_dir": src_dir,
                "dst_dir": dst_dir,
                "names": names,
                "overwrite": bool(overwrite),
                "skip_existing": bool(skip_existing),
                "merge": bool(merge),
            },
        )
        return data if isinstance(data, dict) else {}

    async def remove(self, dir_path: str, names: list[str]) -> None:
        """批量删除。dir_path 是所在目录，names 是目录内的名字。"""
        await self.request(
            "POST", "/fs/remove", json_body={"dir": dir_path, "names": names}
        )

    async def storages(self) -> list[dict[str, Any]]:
        """列出全部存储挂载（需要管理员权限）。"""
        data = await self.request("GET", "/admin/storage/list")
        content = (data or {}).get("content") if isinstance(data, dict) else None
        if not isinstance(content, list):
            return []
        return [item for item in content if isinstance(item, dict)]

    async def offline_download_tools(self, path: str) -> list[str]:
        """列出指定目录下可用的离线下载方式名称。"""
        data = await self.request(
            "GET", "/public/offline_download_tools", params={"path": path}
        )
        if not isinstance(data, list):
            return []
        return filter_offline_tools(data)

    async def add_offline_download(
        self,
        urls: list[str],
        path: str,
        tool: str = "",
        delete_policy: str = "delete_on_upload_succeed",
    ) -> list[dict[str, Any]]:
        """提交离线下载，返回服务端创建的任务列表。"""
        data = await self.request(
            "POST",
            "/fs/add_offline_download",
            json_body={
                "urls": urls,
                "path": path,
                "tool": tool,
                "delete_policy": delete_policy,
            },
        )
        tasks = (data or {}).get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks, list):
            return []
        return [t for t in tasks if isinstance(t, dict)]

    async def list_tasks(
        self, which: str = "undone", kind: str = "offline_download"
    ) -> list[dict[str, Any]]:
        if which not in ("undone", "done"):
            raise OpenListConfigError(f"未知的任务列表类型：{which}")
        if kind not in TASK_KINDS:
            raise OpenListConfigError(f"未知的任务类别：{kind}")
        data = await self.request("GET", f"/task/{kind}/{which}")
        if not isinstance(data, list):
            return []
        return [t for t in data if isinstance(t, dict)]

    async def task_info(
        self, task_id: str, kind: str = "offline_download"
    ) -> dict[str, Any]:
        data = await self.request(
            "POST", f"/task/{kind}/info", params={"tid": task_id}
        )
        return data if isinstance(data, dict) else {}

    async def task_action(
        self, action: str, task_id: str = "", kind: str = "offline_download"
    ) -> None:
        if action == "clear_done":
            await self.request("POST", f"/task/{kind}/clear_done")
            return
        if action not in ("cancel", "retry", "delete"):
            raise OpenListConfigError(f"未知的任务操作：{action}")
        if not task_id:
            raise OpenListConfigError("该操作需要提供 task_id")
        await self.request(
            "POST", f"/task/{kind}/{action}", params={"tid": task_id}
        )


async def _loads(text: str) -> Any:
    import json

    return json.loads(text)
