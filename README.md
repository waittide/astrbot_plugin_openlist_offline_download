# OpenList 离线下载与文件管理（AstrBot 插件）

把 OpenList 的离线下载和文件管理能力接进 AstrBot：直接把链接丢给机器人，它会提交到 OpenList 离线下载并持续查询进度，任务结束后主动推送结果；也可以让机器人列出、移动、复制、删除、重命名网盘里的文件和目录。

支持 `http/https`、BT 磁力链（`magnet:?`）、`ed2k`、`ftp`、`thunder` 等所有 OpenList 离线下载工具支持的链接。

## 特性

- **自然语言**：直接说「帮我离线下载 <链接>」「看看 /115/临时 里有什么」「把 a.mkv 改名成 b.mkv」，机器人调用对应工具。保存路径、下载方式、服务地址与认证方式全部只读配置项，模型无法指定也无法编造。
- **两种认证**：访问密钥（`Authorization` 头）或用户名 + 密码（自动登录并缓存 JWT）。
- **完成推送**：提交后后台轮询任务状态，任务成功/失败时把结果推回原会话，无需再问一次。
- **文件管理**：列表 / 移动 / 复制 / 删除 / 重命名 / 新建目录，危险操作默认二次确认。
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
| `download.download_tool` | 离线下载方式，**下拉框选择**：`auto`（自动选择该路径可用的第一个）/ `115 Open` / `115 Cloud` / `123 Open` / `123Pan` / `GuangYaPan` / `PikPak` / `Thunder` / `ThunderX` / `ThunderBrowser` / `aria2` / `qBittorrent` / `Transmission`。`SimpleHttp`（先下到 OpenList 本机再上传）不在支持范围内，不会出现在下拉框里，也不会被自动选中 |
| `download.delete_policy` | 临时文件清理策略：`delete_on_upload_succeed` / `delete_on_upload_failed` / `delete_never` / `delete_always` / `upload_download_stream` |
| `download.max_urls` | 单次最多提交链接数 |
| `notify.enabled` | 任务结束后是否主动推送结果 |
| `notify.interval` | 后台轮询间隔（秒） |
| `notify.expire_hours` | 单个任务最长跟踪时长（小时），超时后停止推送 |
| `download.admin_only` | 仅管理员可提交离线下载任务（含取消/重试/删除任务记录）。与「仅管理员可修改文件」是两个独立开关 |
| `advanced.confirm_destructive` | 文件操作二次确认：删除/移动/重命名（覆盖）前先返回清单等待确认 |
| `advanced.admin_only_file_ops` | 仅管理员可修改 OpenList 文件（新建目录/移动/复制/重命名/删除）。与「仅管理员可提交下载任务」分开控制 |

> 访问密钥在 OpenList 里以 `Authorization: <token>` 形式使用，**不加 `Bearer` 前缀**。

> 提示：如果机器人在自然语言对话里「看不到」本插件的工具，请到 AstrBot 管理面板 → 配置 → 插件 → 可用插件 里勾选本插件（对应 `cmd_config.json` 的 `plugin_set`）。未勾选时 AstrBot 会在组装请求时过滤掉该插件的工具。

## 使用

自然语言：

```
帮我离线下载 https://example.com/big.iso
把 magnet:?xt=urn:btih:... 离线下载
现在有哪些离线下载任务？
任务 <task_id> 进度怎么样了
取消任务 <task_id>

看看 /115/临时 里有什么
把 /115/临时/a.mkv 移动到 /115/整理
把 /115/临时/a.mkv 改名成 b.mkv
在 /115/临时 下新建目录 下载
删掉 /115/临时/old.txt
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

/ol ls [路径]                列出目录（默认根目录）
/ol mkdir <路径>             新建目录
/ol mv <源…> <目标目录>      移动
/ol cp <源…> <目标目录>      复制
/ol rm <路径…>               删除（目录会连内容一起删）
/ol rn <路径> <新名字>       重命名
```

路径带空格请用引号括起来。危险操作（删除、移动、重命名覆盖）会先返回待确认清单，回复「确认」后 AI 才带 `confirm=True` 重新调用。

指令受 `download.admin_only` / `advanced.admin_only_file_ops` 分别控制（默认都仅管理员）。

## 实现说明

- `openlist_client.py`：OpenList HTTP 客户端，封装 `/api/auth/login`、`/api/me`、`/api/fs/list|get|mkdir|move|copy|remove|rename`、`/api/fs/add_offline_download`、`/api/task/offline_download/*`、`/api/public/offline_download_tools`，含 JWT 缓存与 401 重登。
- `main.py`：插件主体，注册 6 个 LLM 工具（`openlist_status` / `openlist_offline_download` / `openlist_tasks` / `openlist_task_detail` / `openlist_task_action` / `openlist_file_ops`）与 `/ol` 指令组，并维护后台轮询任务结束后推送。

### 已知的平台行为

- OpenList 的 `fs/remove` 是异步的，连续删同一目录可能返回 `操作尚未执行完成，请稍后再试`，稍等再试即可。
- 115 等驱动对同一链接重复提交会拒绝（`code: 10008`），插件如实透传上游错误。
- 部分驱动（如 115 Open）的目录列表有缓存延迟，插件列表时带 `refresh=true`；无刷新权限的账号会自动退回普通列表。

## License

AGPL-3.0
