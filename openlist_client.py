"""OpenList HTTP 客户端（离线下载与任务相关接口）。

只依赖 aiohttp（AstrBot 自带），认证支持两种方式：
- ``token``：访问密钥，直接放进 ``Authorization`` 请求头（裸值，不带 Bearer 前缀）。
- ``password``：用户名 + 密码，先 ``POST /api/auth/login`` 换取 JWT，再放入同一个请求头；
  JWT 会缓存，遇到 401 自动重新登录一次。
"""

from __future__ import annotations

import time
from typing import Any

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
                if resp.status in (401, 403):
                    raise OpenListAuthError(
                        f"OpenList 认证失败（HTTP {resp.status}），请检查访问密钥或用户名密码",
                    )
                try:
                    body = await _loads(text)
                except ValueError:
                    raise OpenListAPIError(
                        f"OpenList 返回了非 JSON 响应（HTTP {resp.status}）：{text[:200]}",
                    ) from None
                if resp.status >= 400:
                    raise OpenListAPIError(
                        f"OpenList 请求失败（HTTP {resp.status}）："
                        f"{body.get('message') or text[:200]}",
                        body.get("code"),
                    )
                code = body.get("code")
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

    async def list_dir(self, path: str = "/", page: int = 1, per_page: int = 100) -> list[dict[str, Any]]:
        """列出目录内容。"""
        path = (path or "/").strip() or "/"
        data = await self.request(
            "POST",
            "/fs/list",
            json_body={
                "path": path,
                "page": max(1, int(page or 1)),
                "per_page": max(1, int(per_page or 100)),
                "refresh": False,
                "password": "",
            },
        )
        content = (data or {}).get("content") if isinstance(data, dict) else None
        if not isinstance(content, list):
            return []
        return [item for item in content if isinstance(item, dict)]

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
        return [str(item) for item in data]

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

    async def list_tasks(self, which: str = "undone") -> list[dict[str, Any]]:
        if which not in ("undone", "done"):
            raise OpenListConfigError(f"未知的任务列表类型：{which}")
        data = await self.request("GET", f"/task/offline_download/{which}")
        if not isinstance(data, list):
            return []
        return [t for t in data if isinstance(t, dict)]

    async def task_info(self, task_id: str) -> dict[str, Any]:
        data = await self.request(
            "POST", "/task/offline_download/info", params={"tid": task_id}
        )
        return data if isinstance(data, dict) else {}

    async def task_action(self, action: str, task_id: str = "") -> None:
        if action == "clear_done":
            await self.request("POST", "/task/offline_download/clear_done")
            return
        if action not in ("cancel", "retry", "delete"):
            raise OpenListConfigError(f"未知的任务操作：{action}")
        if not task_id:
            raise OpenListConfigError("该操作需要提供 task_id")
        await self.request(
            "POST", f"/task/offline_download/{action}", params={"tid": task_id}
        )


async def _loads(text: str) -> Any:
    import json

    return json.loads(text)
