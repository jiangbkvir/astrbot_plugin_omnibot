# astrbot_plugin_omnibot

把 AstrBot 接成 OmniBot 的 **QQ 传输网关**。

本插件只负责消息传输，不实现绑定、签到、转账、抽奖等业务。所有业务命令、
权限、金额和账务仍由 OmniBot 处理。OmniBot 代码不需要任何修改。

## 双向链路

| 方向 | 发起方 | 地址 | 作用 |
| --- | --- | --- | --- |
| 入站 | AstrBot 插件 | `POST {omnibot_site_url}/api/omnibot/qq/webhook` | 把 QQ 消息交给 OmniBot |
| 出站 | OmniBot | `GET {gateway}/api/check` | 连通性检查 |
| 出站 | OmniBot | `POST {gateway}/api/send_group_msg` | 主动发群消息 |
| 出站 | OmniBot | `POST {gateway}/api/send_private_msg` | 主动发私聊消息 |

普通 QQ 指令只走入站请求，OmniBot 在 HTTP 响应中返回 `{"reply":"..."}`，
插件直接代发，因此 **远程 OmniBot 不需要反向访问你的本机**。

只有需要站内信、后台通知等无入站触发场景时，才开启
`outbound_gateway_enabled`。此时插件通过 aiohttp 单独监听端口，默认 `8080`，
与 OmniBot `qq_bot_api_base` 的默认值一致。没有使用 AstrBot 的 Dashboard
Web API，避免把安装/卸载插件的高权限 API Key 交给 OmniBot。

## 安全边界

- `qq_bot_secret` 必填。为空时入站不转发，出站接口全部返回 401。
- 出站接口使用 `X-API-Key`，用 `hmac.compare_digest` 定长比较。
- 入站 webhook 的 `_secret` 与 OmniBot 后台密钥一致。
- `aiocqhttp` 从协议端真实消息段重建 CQ 码，不使用用户输入的伪 CQ 文本。
- `qq_official` 只从平台消息链中读取真实 At；用户手打的 `[CQ:...]` 会被中和。
- 插件不保存账号、金额或转账状态，不执行任何业务命令。

## 支持平台

| 平台 | AstrBot 类型 | 说明 |
| --- | --- | --- |
| NapCat / LLOneBot / Lagrange | `aiocqhttp` | 数字群号、数字 QQ，保留真实 CQ 码 |
| QQ 官方机器人 | `qq_official` | 群为 `group_openid`，成员为 `member_openid` |

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
| `outbound_gateway_enabled` | `false` | 是否开放 OmniBot 反向回调；普通指令无需开启 |
| `omnibot_site_url` | `http://127.0.0.1:8004` | NexusPHP 根地址，不带结尾 `/` |
| `qq_bot_secret` | 空 | 必须与 OmniBot 的「QQ 安全密钥」一致 |
| `gateway_host` | `127.0.0.1` | 回调监听地址；跨内网容器时按需调整 |
| `gateway_port` | `8080` | 与 OmniBot 的「QQ 服务地址」对应 |
| `allowed_groups` | `[]` | 本地入站群白名单，留空交给 OmniBot 判断 |
| `forward_private` | `true` | 是否转发私聊 |
| `request_timeout` | `10` | 请求 OmniBot 的超时秒数 |
| `stop_event_after_forward` | `true` | 转发后阻止 AstrBot 继续处理 |
| `reply_from_webhook_json` | `true` | 直接代发响应里的 `reply` |
| `reply_prefix` | 空 | 代发回复统一前缀 |

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
  "omnibot_site_url": "https://www.tangpt.top",
  "qq_bot_secret": "替换为与 OmniBot 一致的安全密钥",
  "gateway_host": "127.0.0.1",
  "gateway_port": 18080,
  "allowed_groups": [],
  "forward_private": true,
  "request_timeout": 10,
  "stop_event_after_forward": true,
  "reply_from_webhook_json": true,
  "reply_prefix": ""
}
```

## 网络

- 普通指令：AstrBot 主动访问 OmniBot 站点，不需要 OmniBot 访问本机。
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
