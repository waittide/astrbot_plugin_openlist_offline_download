# OpenList 离线下载（AstrBot 插件）

把 OpenList 的离线下载能力接进 AstrBot：直接把链接丢给机器人，它会提交到 OpenList 离线下载，并持续查询任务进度，任务结束后主动推送结果。

支持 `http/https`、BT 磁力链（`magnet:?`）、`ed2k`、`ftp`、`thunder` 等所有 OpenList 离线下载工具支持的链接。

## 特性

- **自然语言**：直接说「帮我离线下载 <链接>」，机器人调用 `openlist_offline_download` 工具提交。保存路径与下载方式只读配置项，模型无法指定也无法编造。
- **两种认证**：访问密钥（`Authorization` 头）或用户名 + 密码（自动登录并缓存 JWT）。
- **完成推送**：提交后后台轮询任务状态，任务成功/失败时把结果推回原会话，无需再问一次。
- **指令兜底**：`/ol` 指令组覆盖全部功能，模型不可用或不想走 LLM 时照样可用。

## 安装

把本仓库放进 AstrBot 的 `data/plugins/` 目录（或在插件市场搜索 `astrbot-plugin-openlist-offline-download`），然后在管理面板启用插件。

## 配置

管理面板 → 插件 → OpenList 离线下载 → 配置。

| 配置项 | 说明 |
| --- | --- |
| `connection.base_url` | OpenList 服务地址，如 `https://openlist.example.com`（可省略 `/api` 后缀）。**必填** |
| `connection.auth_mode` | `password`（用户名密码）或 `token`（访问密钥） |
| `connection.username` / `connection.password` | `auth_mode=password` 时使用。多因素登录的账号请改用访问密钥 |
| `connection.access_token` | `auth_mode=token` 时使用。OpenList 后台 → 设置 → 其他 → 访问密钥 里生成 |
| `connection.verify_tls` | 自签证书时关闭 |
| `connection.timeout` | 单次请求超时（秒） |
| `download.download_path` | 离线下载保存路径，如 `/115/离线下载`。必须是 OpenList 里真实存在的目录 |
| `download.download_tool` | 离线下载方式，如 `115 Open`、`SimpleHttp`、`aria2`、`qBittorrent`。留空则由插件自动选择该路径可用的第一个 |
| `download.delete_policy` | 临时文件清理策略：`delete_on_upload_succeed` / `delete_on_upload_failed` / `delete_never` / `delete_always` / `upload_download_stream` |
| `download.max_urls` | 单次最多提交链接数 |
| `notify.enabled` | 任务结束后是否主动推送结果 |
| `notify.interval` | 后台轮询间隔（秒） |
| `notify.expire_hours` | 单个任务最长跟踪时长（小时），超时后停止推送 |
| `advanced.admin_only` | 仅管理员可提交/取消离线下载任务 |

> 访问密钥在 OpenList 里以 `Authorization: <token>` 形式使用，**不加 `Bearer` 前缀**。

## 使用

自然语言：

```
帮我离线下载 https://example.com/big.iso
把 magnet:?xt=urn:btih:... 离线下载
现在有哪些离线下载任务？
任务 <task_id> 进度怎么样了
取消任务 <task_id>
```

指令：

```
/ol help                     查看帮助
/ol status                   服务连通性、认证、下载路径与可用方式
/ol dl <链接…>               提交离线下载（支持多个链接）
/ol tasks                    列出进行中的任务
/ol done                     列出已结束的任务
/ol detail <task_id>         任务详情
/ol cancel <task_id>         取消任务
/ol retry <task_id>          重试任务
/ol delete <task_id>         删除任务记录
/ol clear                    清空已结束的任务
```

指令受 `advanced.admin_only` 控制（默认仅管理员）。

## 实现说明

- `openlist_client.py`：OpenList HTTP 客户端，封装 `/api/auth/login`、`/api/me`、`/api/fs/list`、`/api/fs/add_offline_download`、`/api/task/offline_download/*`，含 JWT 缓存与 401 重登。
- `main.py`：插件主体，注册 5 个 LLM 工具与 `/ol` 指令组，并维护后台轮询任务结束后推送。

## License

AGPL-3.0
