# astrbot_plugin_omnibot

把 AstrBot 接成 OmniBot 的 **QQ 传输网关**。

本插件只负责消息传输，不实现绑定、签到、转账、抽奖等业务。所有业务命令、
权限、金额和账务仍由 OmniBot 处理。

## 双向链路

| 方向 | 发起方 | 地址 | 作用 |
| --- | --- | --- | --- |
| 入站 | AstrBot 插件 | `POST {omnibot_site_url}/api/omnibot/qq/webhook` | 把 QQ 消息交给 OmniBot |
| 指令面板 | AstrBot 插件 | `GET {omnibot_site_url}/api/omnibot/qq/commands` | 拉取 OmniBot 当前启用的 QQ 指令 |
| 开奖结果 | AstrBot 插件 | `POST {omnibot_site_url}/api/omnibot/qq/outbox/{watch,ack}` | 仅查询本次下注产生的活动目标 |
| 指令面板 | AstrBot 插件 | `POST/PUT {api.bot.qq.com}/v2/panels` | 创建或更新 QQ 官方群指令面板 |
| 出站 | OmniBot | `GET {gateway}/api/check` | 连通性检查 |
| 出站 | OmniBot | `POST {gateway}/api/send_group_msg` | 主动发群消息 |
| 出站 | OmniBot | `POST {gateway}/api/send_private_msg` | 主动发私聊消息 |

普通 QQ 指令只走入站请求，OmniBot 在 HTTP 响应中返回 `{"reply":"..."}`，
插件直接代发，因此 **远程 OmniBot 不需要反向访问你的本机**。

猜大小下注成功时，webhook 响应会额外带有
`_omnibot.guess_result_watch`。插件只把该目标加入临时监听，并按服务端给出的
`next_check_at` 在开奖前后查询；没有下注、下注失败、结果已送达或超过截止时间时，
不会继续请求主站。

只使用 OmniBot 时不需要在 AstrBot 中配置大模型。保持
`stop_event_after_forward=true`，插件会接管 QQ 消息；即使 OmniBot 超时或鉴权失败，
也不会回落到 AstrBot 的 LLM 并弹出“未找到任何可用对话模型”。

只有需要站内信、后台通知等无入站触发场景时，才开启
`outbound_gateway_enabled`。此时插件通过 aiohttp 单独监听端口，默认 `8080`，
与 OmniBot `qq_bot_api_base` 的默认值一致。没有使用 AstrBot 的 Dashboard
Web API，避免把安装/卸载插件的高权限 API Key 交给 OmniBot。

## 安全边界

- `qq_bot_secret` 必填。为空时入站不转发，出站接口全部返回 401。
- 出站接口使用 `X-API-Key`，用 `hmac.compare_digest` 定长比较。
- 入站 webhook 的 `_secret` 与 OmniBot 后台密钥一致。
- `onebot` 解析分支适用于 NapCat、LLOneBot、Lagrange，统一从协议端真实
  OneBot v11 消息段重建 CQ 码，不使用用户输入的伪 CQ 文本；群消息被
  `@机器人 /命令` 时会移除机器人自身 At，保留对其他群成员的 At。
- `qq_official` 只从平台消息链中读取真实 At；用户手打的 `[CQ:...]` 会被中和。
- 插件不保存账号、金额或转账状态，不执行任何业务命令。

## 支持平台

| 平台 | AstrBot 类型 | 说明 |
| --- | --- | --- |
| NapCat / LLOneBot / Lagrange | `aiocqhttp` | 数字群号、数字 QQ，保留真实 CQ 码 |
| QQ 官方机器人 | `qq_official` | 群为 `group_openid`，成员为 `member_openid` |

NapCat、LLOneBot、Lagrange 都实现 OneBot v11，在 AstrBot 中使用同一个
`aiocqhttp` 平台适配器接入，不需要为三个协议端分别安装插件。

### 切换消息解析器

`message_parser` 支持：

| 值 | 行为 |
| --- | --- |
| `auto` | 默认。根据 AstrBot 平台名和消息结构自动选择 OneBot 或 QQ 官方解析 |
| `onebot` | 强制按 OneBot v11 解析；适用于 NapCat、LLOneBot、Lagrange |
| `qq_official` | 强制按 QQ 官方 openid 消息解析 |

为兼容常见写法，也接受 `aiocqhttp`、`onebot_v11`、`napcat`、`llonebot`、
`lagrange` 作为 `onebot` 别名。一般保持 `auto` 即可；存在多个机器人或平台名
识别异常时再手动指定。

QQ 官方平台没有数字 QQ。插件只负责把平台真实消息原样映射成 OneBot v11
事件，不替 OmniBot 发明 QQ 号、别名或绑定关系。

## 安装

在 AstrBot WebUI 的插件页上传 `astrbot_plugin_omnibot.zip`，或把目录放到：

```text
AstrBot/data/plugins/astrbot_plugin_omnibot/
```

目录结构：

```text
astrbot_plugin_omnibot/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
└── OMNIBOT-GATEWAY-API.md
```

## 配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 关闭后停止转发，网关返回 503 |
| `message_parser` | `auto` | `auto` / `onebot` / `qq_official`；NapCat、LLOneBot、Lagrange 使用 `onebot` |
| `outbound_gateway_enabled` | `false` | 是否开放 OmniBot 反向回调；普通指令无需开启 |
| `guess_result_watch_enabled` | `true` | 接收猜大小成功下注对应的开奖结果 |
| `guess_result_watch_retry_seconds` | `10` | 开奖延迟或发送失败时的复查间隔，最小 3 秒 |
| `omnibot_site_url` | `http://127.0.0.1:8004` | NexusPHP 根地址，不带结尾 `/` |
| `qq_bot_secret` | 空 | 必须与 OmniBot 的「QQ 安全密钥」一致 |
| `gateway_host` | `127.0.0.1` | 回调监听地址；跨内网容器时按需调整 |
| `gateway_port` | `8080` | 与 OmniBot 的「QQ 服务地址」对应 |
| `allowed_groups` | `[]` | 本地入站群白名单，留空交给 OmniBot 判断 |
| `qq_group_id_map` | `{}` | QQ 官方 `group_openid` 到 OmniBot 数字群号的映射 |
| `command_panel_sync_enabled` | `true` | 自动同步 QQ 官方群指令面板 |
| `command_panel_fallback_enabled` | `true` | OmniBot 指令清单接口不可用时，使用内置常用指令清单注册面板 |
| `command_panel_sync_interval` | `300` | 指令面板同步间隔，最小 30 秒 |
| `command_panel_target_type` | `all` | `all` 全群生效，`specific` 仅指定群生效 |
| `command_panel_group_ids` | `[]` | `specific` 模式的目标群，可填 openid 或映射中的数字群号 |
| `command_panel_platform_id` | 空 | 多机器人时指定 AstrBot 的 QQ 官方平台 ID |
| `command_panel_remark` | `OmniBot 自动同步指令面板` | 面板内部标识，不展示给群成员 |
| `command_panel_max_items` | `20` | QQ 面板最多展示的指令数量 |
| `forward_private` | `true` | 是否转发私聊 |
| `request_timeout` | `10` | 请求 OmniBot 的超时秒数 |
| `stop_event_after_forward` | `true` | 进入转发流程后阻止 AstrBot 的 AI/其他插件处理，失败时也不回落到大模型 |
| `reply_from_webhook_json` | `true` | 直接代发响应里的 `reply` |
| `reply_prefix` | 空 | 代发回复统一前缀 |
| `reply_markdown_enabled` | `true` | 把 OmniBot 纯文本模板重排为 Markdown（标题、表格、列表） |
| `reply_markdown_platforms` | `["qq_official"]` | 启用 Markdown 的平台；QQ 官方可直接渲染，OneBot 等客户端通常不渲染 |
| `reply_markdown_max_rows` | `40` | 表格/列表最多渲染行数，超出显示省略说明 |
| `reply_markdown_lottery_max_rows` | `5` | 抽奖中奖记录默认展示条目数；设为 `0` 可全部折叠，完整记录仍发送到站内信 |

### 猜大小开奖结果

- 只在 `/guess 大|小 金额` 下注成功后登记群或私聊目标。
- 首次查询安排在预计开奖时间附近；开奖延迟时最多每 10 秒复查一次。
- 同一群多人下注只生成一条群结果，列出本群每位参与者的下注和输赢。
- 私聊通知只包含本人结果；群和私聊分别下注会收到各自通知。
- 结果送达并 ACK 后立即移除目标；`draw_at + 90 秒` 后停止查询。
- AstrBot 重启后临时目标不会恢复；用户再次发送 `/guess` 可重新获得活动目标。
- 主动通知未送达时，可用 `/guess` 查看“我的上期结果”。

### 回复排版

OmniBot 的回复模板是 `────` 分隔线 + `键：值` 的纯文本，在 QQ 里没有任何排版。
插件会按语义重排为 Markdown，然后以 QQ 的 markdown 消息直接发送：

- 单句错误、状态和普通提示保持原文，不强行加标题
- 只有 4 项以上、内容较短的 `键：值` 数据才转成两列表格；1-3 项改为键值列表
- `•` 项目转成列表，多条 `/命令` 转成指令表；说明文字保持普通段落
- `────` 分隔线只用于分段，不会把状态说明误判成新标题
- QQ 官方 Markdown 不支持 HTML 可点击折叠；抽奖记录默认只展示前 5 项，其余显示为已折叠，完整记录保留在站内信

如果 OmniBot 后台模板本身已经输出 Markdown（含标题、表格、列表等语法），插件会
原样透传，不会二次加工。

发送保护：超过 3500 字符会截断并追加提示；若平台拒绝 markdown，插件会用 OmniBot
原文重发一次纯文本，保证回复不丢。

### OmniBot 后台

| 配置 | 示例 |
| --- | --- |
| QQ 服务地址 | `http://127.0.0.1:8080` |
| QQ 安全密钥 | 与插件 `qq_bot_secret` 完全相同 |
| QQ 群白名单 | `aiocqhttp` 填数字群号；`qq_official` 填 `group_openid` |

当前本地示例：

```json
{
  "enabled": true,
  "outbound_gateway_enabled": false,
  "guess_result_watch_enabled": true,
  "guess_result_watch_retry_seconds": 10,
  "omnibot_site_url": "https://www.tangpt.top",
  "qq_bot_secret": "替换为与 OmniBot 一致的安全密钥",
  "gateway_host": "127.0.0.1",
  "gateway_port": 18080,
  "allowed_groups": [],
  "qq_group_id_map": {
    "QQ官方群openid": "数字群号"
  },
  "command_panel_sync_enabled": true,
  "command_panel_sync_interval": 300,
  "command_panel_target_type": "specific",
  "command_panel_group_ids": ["数字群号"],
  "command_panel_platform_id": "",
  "command_panel_remark": "OmniBot 自动同步指令面板",
  "command_panel_max_items": 20,
  "forward_private": true,
  "request_timeout": 10,
  "stop_event_after_forward": true,
  "reply_from_webhook_json": true,
  "reply_prefix": ""
}
```

## QQ 官方指令面板

OmniBot 新增只读接口：

```text
GET /api/omnibot/qq/commands
X-API-Key: qq_bot_secret
```

接口只返回 OmniBot 当前启用、且支持 QQ 平台的斜杠指令。AstrBot 插件会：

1. 使用 AstrBot 已配置的 QQ 官方 `AppID/AppSecret` 获取 `access_token`。
2. 拉取 OmniBot 指令清单。
3. 按 `command_panel_remark` 查找已有面板。
4. 自动调用 QQ `/v2/panels` 创建或更新面板。
5. 在 `specific` 模式下通过 `qq_group_id_map` 将数字群号反查为
   `group_openid`，再同步 `/v2/panels/{panel_id}/target`。

后台修改指令开关、别名或描述后，无需重启 AstrBot；插件会在下个同步周期
自动更新 QQ 指令面板。QQ 官方只提供 `group_openid` 和 `member_openid`，
群级映射由插件自动完成，不需要手动维护成员 openid 映射。
QQ 面板名称和描述按官方宽度限制裁剪（中文占 2、英文占 1：名称最多 14、
描述最多 30），避免新增较长后台说明时返回 `30013 超出数量限制`。
对 `/bind`、`/lottery`、`/guess`、`/fl`、`/rp` 等需要参数的指令，面板描述
会优先显示可直接照抄的用法；OmniBot 返回的短描述过于简略时，插件也会使用
内置用法补充，避免只显示“发红包”“/猜大小”而不知道下一步怎么填。
点击面板指令后，QQ 会自动在消息最前面加上 `@机器人`；插件只移除机器人自己的
At，保留对其他成员的 At，再以 `/指令` 文本转发给 OmniBot。

## 网络

- 普通指令和猜大小结果：AstrBot 主动访问 OmniBot 站点，不需要 OmniBot 访问本机。
- 主动推送且同主机：OmniBot 访问 `http://127.0.0.1:8080`。
- 主动推送且 Docker：OmniBot 访问 `http://host.docker.internal:8080`。
- 同 Docker network：使用服务名，例如 `http://astrbot:8080`。

OmniBot 主动发送前，目标群或用户必须先发过一条消息，让插件建立会话映射。
单条消息上限 1 MiB。

## 自检

仅在开启 `outbound_gateway_enabled` 后执行：

```bash
curl -i -H "X-API-Key: 你的密钥" http://127.0.0.1:8080/api/check
```

期望：

```json
{"status":"ok","plugin":"astrbot_plugin_omnibot","groups":1,"users":1}
```

密钥为空或不正确时返回：

```json
{"error":"Unauthorized"}
```

## 常见问题

- **401**：`qq_bot_secret` 为空，或两边密钥不一致。
- **端口占用**：修改 `gateway_port`，同步修改 OmniBot「QQ 服务地址」。
- **NapCat / LLOneBot / Lagrange 不转发**：确认 AstrBot 已启用 `aiocqhttp`
  （OneBot v11）平台并成功反向连接，再将 `message_parser` 设为 `onebot`。
  这三个协议端不需要单独选择平台类型。
- **OmniBot 测试连接失败但指令可用**：这是正常的。`outbound_gateway_enabled=false`
  时不开放 `/api/check`；普通指令仍通过 webhook `reply` 工作。
- **主动推送 404**：目标会话还没有入站记录，先让用户或群发一条消息。
- **QQ 官方没有触发转账**：这是预期行为。插件不实现转账，也不会把 openid
  伪造成数字 QQ 或 CQ 码；需要由 OmniBot 的业务层适配 QQ 官方身份模型。
- **不要把出站网关暴露到公网**：优先绑定 `127.0.0.1`；跨容器时只开放内网
  网段，并在 OmniBot 侧使用同一把高熵密钥。微信/QQ 号、openid 都不应当作
  身份凭证。
- **回复重复**：`reply_from_webhook_json` 和 OmniBot 主动推送只保留一路。

## 文档

OmniBot 网关字段、鉴权、响应和调试约束见
[OMNIBOT-GATEWAY-API.md](OMNIBOT-GATEWAY-API.md)。

实现参考 AstrBot 官方插件开发文档：
<https://docs.astrbot.app/dev/star/plugin-new.html>
