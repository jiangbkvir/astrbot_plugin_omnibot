# OmniBot QQ 网关对接接口文档

> 面向 AI coding / 二次开发。所有结论均来自源码，标注了文件与行号，可逐条核对。
>
> 源码基线：`nexusphp-1.9.11/plugins/OmniBot/`（plugin.json `version: 1.0.0`）
> 文档目标：**实现一个 OmniBot 认可的 QQ 机器人网关**，不改动 OmniBot 任何代码。
> 最新实现：`astrbot_plugin_omnibot` 1.0.1；只负责传输，不实现任何业务命令。

## AstrBot 平台适配差异

`astrbot_plugin_omnibot` 只做传输，支持 NapCat / LLOneBot / Lagrange
（AstrBot 的 `aiocqhttp` OneBot v11 适配器）和 `qq_official`：

| 项目 | `onebot`（NapCat / LLOneBot / Lagrange） | `qq_official` |
|---|---|---|
| 群标识 | 数字群号 | `group_openid` 字符串 |
| 用户标识 | 数字 QQ 号 | `member_openid` / `user_openid` |
| `raw_message` | 真实消息段重建 CQ 码 | 真实 At 段重建；手打 CQ 码中和 |
| 普通 `/命令` | 原样转发 | 原样转发 |
| `@成员 +100` | 原样转发给 OmniBot | 只转发平台真实 At，不做业务适配 |

插件配置 `message_parser=auto` 时按 AstrBot 平台自动识别；也可强制设为
`onebot` 或 `qq_official`。NapCat、LLOneBot、Lagrange 在 AstrBot 中统一使用
`aiocqhttp` 平台类型，不需要分别适配。

QQ 官方平台没有数字 QQ，和 OmniBot 当前按数字 QQ 绑定的业务模型不同。
这是 **OmniBot 业务层与 QQ 官方身份模型之间的差异**，不属于传输网关职责。
本插件不会把 openid 伪造成 QQ 号、不会生成转账 CQ 码、不会做二次确认或限额。
如果要在 QQ 官方群使用 OmniBot 的转账，需要在 OmniBot 侧设计身份映射方案。

---

## 0. 一分钟速览

OmniBot 把「QQ 机器人」抽象成一个 HTTP 网关，双向各 2~3 个接口：

```
┌──────────────┐                                    ┌──────────────┐
│              │  ① 入站 POST /api/omnibot/qq/webhook│              │
│  QQ 网关      │ ─────────────────────────────────> │              │
│  (你要实现)   │      body: OneBot v11 消息事件      │   OmniBot    │
│              │      resp: {"reply": "..."}        │  (NexusPHP)  │
│              │                                    │              │
│              │  ② 出站 GET  /api/check             │              │
│              │ <───────────────────────────────── │              │
│              │  ③ 出站 POST /api/send_group_msg    │              │
│              │ <───────────────────────────────── │              │
│              │  ④ 出站 POST /api/send_private_msg  │              │
│              │ <───────────────────────────────── │              │
└──────────────┘                                    └──────────────┘
```

网关只需实现 **4 个 HTTP 端点**：

| # | 方向 | 方法 | 路径 | 鉴权 |
|---|---|---|---|---|
| ① | 网关 → OmniBot | POST | `{站点}/api/omnibot/qq/webhook` | body `_secret` |
| ② | OmniBot → 网关 | GET | `{base}/api/check` | 头 `X-API-Key` |
| ③ | OmniBot → 网关 | POST | `{base}/api/send_group_msg` | 头 `X-API-Key` |
| ④ | OmniBot → 网关 | POST | `{base}/api/send_private_msg` | 头 `X-API-Key` |

> ①由网关主动调用，②③④由网关**监听端口**对外提供。
> OmniBot 侧两个配置项：`qq_bot_api_base`（= `{base}`）和 `qq_bot_secret`。
> 默认 `qq_bot_api_base` 为 `http://127.0.0.1:8080`。
>
> **推荐部署**：普通指令只使用①，回复从①的响应体 `reply` 返回；②③④默认关闭，
> 远程 OmniBot 无需反向访问本机。只有后台主动推送才开启出站回调。

---

## 1. 配置项（OmniBot 侧）

存储表：`plugin_omnibot_settings`（key-value）。

| 配置 key | 类型 | 默认值 | 在网关中的用途 |
|---|---|---|---|
| `qq_bot_api_base` | string | `http://127.0.0.1:8080` | 出站接口根地址 `{base}` |
| `qq_bot_secret` | string | `''` | ①的 body `_secret`，同时作为②③④的 `X-API-Key` |
| `qq_allowed_groups` | array | `[]` | 群白名单，QQ 群消息只处理名单内的群 |
| `site_origin` | string | `config('app.url')` | 转账回调用，与网关无关 |
| `enable_bind` | bool | `true` | `/bind` `/unbind` 开关 |
| `enable_sign` | bool | `true` | `/sign` 开关 |
| `enable_user_info` | bool | `true` | `/me` 开关 |
| `enable_transfer` | bool | `true` | 转账开关 |
| `bind_private_only` | bool | `true` | `/bind` 是否仅限私聊 |
| `command_lottery_enabled` | bool | `false` | `/lottery` 开关 |
| `enable_guess` | bool | `false` | `/guess` `/猜大小` 开关，默认关闭 |
| `tpl_guess_query` | string | 内置模板 | 猜大小查询回复模板 |
| `tpl_guess_bet_success` | string | 内置模板 | 猜大小下注成功回复模板 |
| `tpl_guess_bet_fail` | string | 内置模板 | 猜大小下注失败回复模板 |

**兼容回退**：读取时按 `qq_bot_api_base` → `bot_api_base`、`qq_bot_secret` → `bot_secret` 的顺序回退
（`src/OmniBot.php:19-20`、`src/Controllers/OmniBotController.php:31`）。

**读取 `{base}` 的确切代码**（`src/OmniBot.php:116-117`）：

```php
$apiUrl = rtrim((string) OmniBotSetting::get('qq_bot_api_base',
    OmniBotSetting::get('bot_api_base', 'http://127.0.0.1:8080')), '/');
$apiKey = (string) OmniBotSetting::get('qq_bot_secret',
    OmniBotSetting::get('bot_secret', ''));
```

注意 `rtrim(..., '/')`：base 结尾的斜杠会被去掉，所以填不填都行，但**不要**在 base 里带 `/api`。

---

## 2. 接口①：入站 Webhook（网关 → OmniBot）

网关收到 QQ 消息后，把消息转成 OneBot v11 事件发给 OmniBot。

### 2.1 请求

```http
POST {site_origin}/api/omnibot/qq/webhook
Content-Type: application/json
```

路由定义（`routes/api.php:6`）：

```php
Route::post('/api/omnibot/qq/webhook', [OmniBotController::class, 'handleQqWebhook'])
    ->name('omnibot.qq.webhook');
```

Body（OneBot v11 `message` 事件）:

```json
{
  "post_type": "message",
  "message_type": "group",
  "sub_type": "normal",
  "self_id": 444555666,
  "user_id": 111222333,
  "group_id": 555666,
  "message_id": 777888,
  "message": "[CQ:at,qq=123456789] +100",
  "raw_message": "[CQ:at,qq=123456789] +100",
  "message_str": "[CQ:at,qq=123456789] +100",
  "time": 1792999471,
  "sender": {
    "user_id": 111222333,
    "nickname": "小明",
    "card": "小明"
  },
  "_secret": "你的密钥"
}
```

### 2.2 字段要求（只列 OmniBot 真正读取的）

解析代码（`src/Controllers/OmniBotController.php:89-105`）：

```php
protected function buildQqContext(array $data): ?array
{
    if (($data['post_type'] ?? '') !== 'message') {
        return null;                      // 非 message 事件直接被丢弃
    }
    return [
        'platform'  => 'qq',
        'chat_type' => ($data['message_type'] ?? '') === 'group' ? 'group' : 'private',
        'chat_id'   => (string) (($data['message_type'] ?? '') === 'group'
                        ? ($data['group_id'] ?? '') : ($data['user_id'] ?? '')),
        'sender_platform_uid' => (string) ($data['user_id'] ?? ''),
        'sender_username'     => null,
        'raw_message'         => trim((string) ($data['raw_message'] ?? '')),
        'reply_target_platform_uid' => null,
        'original_event' => $data,
    ];
}
```

| 字段 | 必须 | 类型 | 说明 |
|---|---|---|---|
| `post_type` | **是** | string | 必须严格等于 `"message"`，否则 `{"status":"ignored"}` |
| `message_type` | **是** | string | `"group"` 或 `"private"`；**其他值一律当 private** |
| `group_id` | 群消息必须 | int/string | 群号，`chat_id` 来源 |
| `user_id` | **是** | int/string | 发送者 QQ，`sender_platform_uid` 来源；私聊时也是 `chat_id` |
| `raw_message` | **是** | string | 核心字段，必须带 CQ 码，见 §2.3 |
| `_secret` | 密钥非空时必须 | string | 严格 `!==` 比较，见 §2.5 |

**可选但建议填**（OmniBot 不读，但便于网关自身调试/日志）：`self_id`、`message_id`、`message`、
`time`、`sender`、`sub_type`。

### 2.3 `raw_message` 必须带 CQ 码（最容易踩的坑）

OmniBot 的命令解析全部基于 `raw_message` 字符串：

1. **命令前缀判定**（`OmniBotController.php:211-217`）
   ```php
   if (!str_starts_with($rawMessage, '/')) {
       return response()->json(['status' => 'ignored_no_prefix']);
   }
   ```
   普通聊天以 `/` 开头才算命令。

2. **转账是特殊命令，完全靠 CQ 码正则**（`CommandRegistry.php:532-535`）
   ```php
   'matchers' => [
       'qq' => '/\[CQ:at,qq=\d+\]\s*\+\s*\d+/',
       'tg' => '/^@[A-Za-z0-9_]{3,}\s*\+\s*\d+$/',
   ],
   ```
   `TransferHandler` 里再解析一次（`src/Handlers/TransferHandler.php:20-22`）：
   ```php
   if (!preg_match('/\[CQ:at,qq=(\d+)\]\s*\+\s*(\d+)/', $message, $matches)) {
       return null;
   }
   $toPlatformUid = (string) $matches[1];
   $amount = (float) $matches[2];
   ```

   所以 `@某人 +100` 在 `raw_message` 里必须是 **`[CQ:at,qq=123456789] +100`** 这种形态。
   如果只给纯文本 `"@某人 +100"`，转账**永远不触发**。

   该正则的匹配边界（已实测）：

   | `raw_message` | 是否匹配 | 说明 |
   |---|---|---|
   | `[CQ:at,qq=123456789] +100` | ✅ | 标准形态 |
   | `[CQ:at,qq=123456789]+100` | ✅ | 无空格 |
   | `[CQ:at,qq=123456789]   +50` | ✅ | 多空格 |
   | `[CQ:at,qq=123456789] + 100` | ✅ | `+` 前后都可用 `\s*` |
   | `你好 [CQ:at,qq=123] +100` | ✅ | `preg_match` 是搜索语义，at 段前可有别的内容 |
   | `[CQ:at,qq=123] 你好 +100` | ❌ | **at 段与 `+` 之间夹了非空白字符** |
   | `@小明 +100` | ❌ | 纯文本，没有 CQ 码 |
   | `[CQ:at,qq=abc] +100` | ❌ | `qq` 必须是纯数字 |

   > 注意 `[CQ:at,qq=123] 你好 +100` 这种「@之后又打字」的常见输入**不会触发转账**。
   > 这是 OmniBot 正则的既有行为，网关侧不要试图「修正」，否则会与 OmniBot 的判断不一致。

3. **命令 token 规范化**（`CommandRegistry.php:677-689`）
   ```php
   $command = strtolower(trim($command));
   if ($command === '' || !str_starts_with($command, '/')) return '';
   if (preg_match('/^(\/[^\s@]+)@[a-z0-9_]{5,}$/', $command, $matches)) {
       $command = $matches[1];   // 去掉 /cmd@botname 的 @botname 后缀
   }
   ```
   命令大小写不敏感；支持 `/help@123456` 这种带机器人后缀的写法。

### 2.4 支持的 QQ 指令（网关只需透传文本，不必自己解析）

| 指令 | 说明 | 需要 CQ 码 |
|---|---|---|
| `/help` | 帮助 | 否 |
| `/bind [密钥]` | 绑定站点账号（默认仅私聊） | 否 |
| `/unbind` | 解绑 | 否 |

绑定规则：
- 密钥**一次性**使用。绑定成功或消费后失败都会立即失效，不能重复提交。
- 一个站点账号最多绑定一个 QQ 和一个 TG。QQ/TG 已绑定其他账号、或目标平台已被占用时拒绝绑定，必须先解绑。
- 解绑会同时清空当前密钥；QQ/TG 都已绑定时不允许再生成密钥，只剩空位时才可生成。
| `/sign` | 每日签到 | 否 |
| `/me` | 查看站点账号信息 | 否 |
| `/lottery [次数]` | 参与抽奖 | 否 |
| `/pool` | 查看奖池 | 否 |
| `/guess [大|小 金额]` | 猜大小查询或下注，无参数查询当前期 | 否 |
| `/猜大小 [大|小 金额]` | `/guess` 的中文别名 | 否 |
| `@成员 +金额` | 转账，最小 100 魔力值 | **是** |

> `/rp`、`/grab`、`/pm_on`、`/pm_off`、`/sync_news` 仅 Telegram 支持，
> QQ 调用会返回 `{"status":"ignored_platform_not_supported"}`（`OmniBotController.php:235-253`）。

群白名单校验（`OmniBotController.php:320-331`）：

```php
if (($context['chat_type'] ?? '') !== 'group') return true;   // 私聊不校验
$chatId = (string) ($context['chat_id'] ?? '');
$allowed = array_map('strval', (array) OmniBotSetting::get('qq_allowed_groups', []));
return in_array($chatId, $allowed, true);
```

私聊**不受** `qq_allowed_groups` 限制。群号比较是**字符串严格比较**。

### 2.5 鉴权：`_secret`

`src/Controllers/OmniBotController.php:28-35`：

```php
$secret = (string) OmniBotSetting::get('qq_bot_secret',
    OmniBotSetting::get('bot_secret', ''));

if ($secret !== '' && ($data['_secret'] ?? '') !== $secret) {
    return response()->json(['error' => 'Unauthorized'], 403);
}
```

- OmniBot 侧密钥为空 → **完全不校验**，`_secret` 可以不带。
- OmniBot 侧密钥非空 → `_secret` 必须**严格相等**（`!==`，类型也要一致，JSON 里是字符串）。
- 失败返回 **403** + `{"error":"Unauthorized"}`。

> 安全提示：OmniBot 自身允许空密钥，但这会让公开 webhook 变成无鉴权接口。
> `astrbot_plugin_omnibot` 在 `qq_bot_secret` 为空时 fail-closed：
> 不转发消息、不提供无鉴权的出站网关。部署时务必配置密钥，
> 且建议至少 16 字节。

### 2.6 响应契约

**重要：QQ 日常指令无需反向回调。** OmniBot 的 QQ webhook 会把业务结果放在
本次 HTTP 200 响应的 `reply` 字段里；网关直接代发即可。不要假设每次回复都会
再调用 `/api/send_group_msg` 或 `/api/send_private_msg`。只有 OmniBot 的后台主动
推送才需要开启回调接口。


成功且有回复（`OmniBotController.php:363-374`，注意 QQ 走 `replyByJson = true`）：

```json
{"reply": "✅ 签到成功，获得 5 个魔力值"}
```

其他状态（均为 HTTP 200，靠 `status` 区分）：

| 响应 body | 含义 | 网关该怎么做 |
|---|---|---|
| `{"reply":"..."}` | 有回复内容 | 把 `reply` 发到 QQ |
| `{"status":"no_match"}` | 命令未匹配/无回复 | 不发消息 |
| `{"status":"ignored"}` | `post_type != message` | 不发消息 |
| `{"status":"ignored_no_prefix"}` | 不以 `/` 开头（非命令） | 不发消息 |
| `{"status":"ignored_chat_not_allowed"}` | 群不在白名单 | 不发消息 |
| `{"status":"ignored_platform_not_supported"}` | 该指令不支持 QQ | 不发消息 |
| `{"status":"not_initialized"}` + **HTTP 503** | OmniBot 数据库未初始化 | 记录错误 |
| `{"error":"Unauthorized"}` + **HTTP 403** | `_secret` 不匹配 | 检查密钥配置 |

> 关键：QQ 平台走 `replyByJson=true`（`OmniBotController.php:43` 传 `true`），
> 回复**只**通过响应体的 `reply` 字段返回，OmniBot 不会再主动调 ③④ 推一次。
> 只有 Telegram 才走 `replyByJson=false` 并通过 send API 主动推送。
> → 因此网关实现时**优先读响应体 `reply`**，③④ 作为可选兜底。

### 2.7 curl 自测

```bash
curl -sS -X POST "http://127.0.0.1:8004/api/omnibot/qq/webhook" \
  -H "Content-Type: application/json" \
  -d '{
    "post_type": "message",
    "message_type": "group",
    "group_id": 555666,
    "user_id": 111222333,
    "message_id": 777888,
    "raw_message": "/help",
    "_secret": "你的密钥"
  }'
```

---

## 3. 接口②：连通性检查（OmniBot → 网关）

### 3.1 请求

```http
GET {base}/api/check
X-API-Key: {qq_bot_secret}
```

调用点（`src/OmniBot.php:17-30`）：

```php
public function checkQqConnection(?string $baseUrl = null, ?string $secret = null): bool
{
    $baseUrl = rtrim((string) ($baseUrl ?: OmniBotSetting::get('qq_bot_api_base', ...)), '/');
    $secret = (string) ($secret ?: OmniBotSetting::get('qq_bot_secret', ...));
    if ($baseUrl === '') return false;
    try {
        return Http::withHeaders(['X-API-Key' => $secret])
            ->timeout(5)
            ->get($baseUrl . '/api/check')
            ->successful();
    } catch (\Throwable $throwable) {
        Log::warning('[OmniBot] QQ connection check failed: ' . $throwable->getMessage());
        return false;
    }
}
```

### 3.2 要求

| 项 | 要求 |
|---|---|
| 方法 | `GET` |
| 路径 | 严格 `/api/check` |
| 请求头 | `X-API-Key: {qq_bot_secret}` |
| 超时 | OmniBot 只等 **5 秒** |
| 判定 | **HTTP 2xx 即成功**，不解析 body |

后台「测试连接」按钮走同一逻辑（`OmniBotManagement.php:804-810`）。

> 如果密钥为空，OmniBot 会发送空 `X-API-Key`。本插件出于安全考虑会对空密钥
> 直接拒绝，因此部署时必须配置同一把非空密钥。

### 3.3 推荐响应

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"status":"ok","plugin":"your-gateway","groups":3,"users":12}
```

body 内容随意，2xx 即可。鉴权失败建议返回 401/403。

---

## 4. 接口③：发群消息（OmniBot → 网关）

### 4.1 请求

```http
POST {base}/api/send_group_msg
Content-Type: application/json
X-API-Key: {qq_bot_secret}

{"group_id":"555666","message":"签到成功"}
```

调用链：`sendPlatformReply()` → `sendQqGroupMsg()` → `postQq()`（`src/OmniBot.php:99-142`）：

```php
public function sendQqGroupMsg(string $groupId, string $message): bool
{
    return $this->postQq('/api/send_group_msg', [
        'group_id' => (string) $groupId,
        'message'  => $message,
    ]);
}

protected function postQq(string $endpoint, array $payload): bool
{
    $apiUrl = rtrim((string) OmniBotSetting::get('qq_bot_api_base', ...), '/');
    $apiKey = (string) OmniBotSetting::get('qq_bot_secret', ...);
    $response = Http::withHeaders([
        'X-API-Key'    => $apiKey,
        'Content-Type' => 'application/json',
    ])->timeout(5)->post($apiUrl . $endpoint, $payload);
    ...
}
```

### 4.2 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `group_id` | string | 群号，**已强制转成字符串** |
| `message` | string | 纯文本回复内容，可能含 `\n` 和 emoji |

### 4.3 要求

| 项 | 要求 |
|---|---|
| 方法 | `POST` |
| 路径 | 严格 `/api/send_group_msg` |
| 头 | `Content-Type: application/json`、`X-API-Key` |
| 超时 | **5 秒** |
| 判定 | **HTTP 2xx 即成功**，不解析 body |

### 4.4 推荐响应

```json
{"status":"ok","data":{"message_id":0}}
```

失败建议带语义化状态码：`400` 参数缺失、`401` 密钥错、`404` 会话未知、`500` 发送失败。

---

## 5. 接口④：发私聊消息（OmniBot → 网关）

与 ③ 完全对称。

```http
POST {base}/api/send_private_msg
Content-Type: application/json
X-API-Key: {qq_bot_secret}

{"user_id":"111222333","message":"请回复 /bind 密钥"}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `user_id` | string | 目标 QQ 号，**已强制转成字符串** |
| `message` | string | 纯文本 |

其余要求、超时、响应同 ③。

> `sendPlatformReply()` 的分派逻辑（`src/OmniBot.php:99-112`）：
> `chat_type === 'group'` → `sendQqGroupMsg`，否则 → `sendQqPrivateMsg`。

---

## 6. 网关实现必须知道的 6 个细节

### 6.1 QQ 走 JSON 回包，不走主动推送

`handleQqWebhook` 调 `handleContext($context, true)`（`OmniBotController.php:43`），
`replyByJson=true` 时 `respond()` 只返回 `{"reply": ...}`（`OmniBotController.php:372-374`），
**不会**调用 ③④。

```
Telegram:  replyByJson=false  →  OmniBot 主动调 send API 推送
QQ:        replyByJson=true   →  回复在 HTTP 响应体里
```

**结论**：QQ 网关的 ③④ 只在「OmniBot 主动发起」的场景才会被调用
（例如站内信推送、红包、公告等后台触发）。日常指令交互只依赖 ①的 `reply`。
因此 `astrbot_plugin_omnibot` 默认 `outbound_gateway_enabled=false`，完全不监听
反向回调端口；需要主动推送时才显式开启。

### 6.2 `{base}` 只到根，不带 `/api`

OmniBot 代码里是 `$apiUrl . '/api/check'` 的拼接方式。
所以 `qq_bot_api_base` 必须是 `http://host:port`，**不能**是 `http://host:port/api`，
否则会请求到 `/api/api/check`。

默认值是 `http://127.0.0.1:8080`，网关监听 8080 即可**零配置对接**。

### 6.3 群白名单是字符串严格比较

```php
array_map('strval', (array) OmniBotSetting::get('qq_allowed_groups', []));
in_array($chatId, $allowed, true);
```

`chat_id` 来自 `(string) $data['group_id']`。
所以 JSON 里传 `"555666"` 或 `555666` 都行，但白名单里存的必须是能转成同样字符串的值。
空白群 `[]` 意味着**所有群都被拒绝**（不是放行）——这是容易误解的点。

### 6.4 私聊不校验白名单

`isAllowedChat()` 第一行：`if chat_type !== 'group' return true`。
私聊永远放行，白名单只管群。

### 6.5 转账无视 `/` 前缀，且先于白名单

`handleContext()` 的处理顺序（`OmniBotController.php:201-217`）：

```
1. matchesSpecialCommand('transfer')  ← 转账           ← 最先
2. isAllowedChat()                     ← 群白名单
3. startsWith('/') 判断                ← 命令前缀
4. 常规命令匹配
```

所以 `@某人 +100` **不带 `/`** 也能触发；但转账内部仍会二次校验白名单
（`dispatchTransfer()` 里有 `isAllowedChat` 检查，`OmniBotController.php:353-355`）。

### 6.6 会写日志文件

`logWebhook()`（`OmniBotController.php:485-492`）会写 `/tmp/omnibot_debug.log`：

```php
file_put_contents('/tmp/omnibot_debug.log',
    date('[Y-m-d H:i:s] ') . "[{$platform}] {$message}\n", FILE_APPEND);
```

排障时先看这个文件，里面有 `INCOMING`、`PARSED CMD=`、`REPLYING`、`NO_MATCH`、`AUTH_FAILED`。

---

## 7. 完整交互时序

### 7.1 用户在群里发 `/sign`

```
用户              QQ协议端          网关             OmniBot
 │   /sign           │               │                 │
 ├──────────────────>│               │                 │
 │                   │  OneBot事件    │                 │
 │                   ├──────────────>│                 │
 │                   │               │ POST webhook    │
 │                   │               ├────────────────>│
 │                   │               │  post_type=message
 │                   │               │  message_type=group
 │                   │               │  group_id/user_id
 │                   │               │  raw_message="/sign"
 │                   │               │  _secret=xxx
 │                   │               │                 │ 校验 _secret
 │                   │               │                 │ 群白名单
 │                   │               │                 │ 解析命令
 │                   │               │                 │ 执行 handler
 │                   │               │ {"reply":"✅..."}│
 │                   │               │<────────────────┤
 │                   │               │ 发消息到 QQ      │
 │                   │<──────────────┤                 │
 │   ✅签到成功       │               │                 │
 │<──────────────────┤               │                 │
```

### 7.2 用户在群里发 `@某人 +100`

```
raw_message = "[CQ:at,qq=123456789] +100"

OmniBot:
  matchesSpecialCommand('transfer')
    → preg_match('/\[CQ:at,qq=\d+\]\s*\+\s*\d+/', raw_message)  ✓
  dispatchTransfer()
    → enable_transfer 检查
    → 群白名单检查（转账内部二次校验）
    → TransferHandler::handle()
        → 再解析一次 at 和金额
        → 校验 amount >= 100
        → 双方都已绑定
        → 不能转给自己
        → 调站点 /mybonus.php 完成扣款
  → {"reply":"✅ [转账成功]..."}
```

**如果 `raw_message` 是纯文本 `"@某人 +100"`，第一步正则就失败，转账不会触发。**

### 7.3 OmniBot 主动推送（后台任务，如站内信）

```
OmniBot ──POST /api/send_private_msg──> 网关 ──> QQ协议端 ──> 用户
             {"user_id":"...","message":"..."}
             头 X-API-Key
```

网关必须自己维护 `user_id → 会话` 的映射（通常从①入站消息里学），
否则不知道往哪个会话发。

---

## 8. 最小实现检查清单

实现网关时逐项确认：

**出站接口（你监听端口）**
- [ ] `GET /api/check` 对正确 `X-API-Key` 返回 2xx
- [ ] `POST /api/send_group_msg` 能按 `group_id` 发到群
- [ ] `POST /api/send_private_msg` 能按 `user_id` 发私聊
- [ ] 三个接口都在 **5 秒**内响应（OmniBot 超时是 5s）
- [ ] 错误时返回非 2xx（OmniBot 会把非 2xx 判为失败）
- [ ] 监听地址 OmniBot 能访问到（跨容器需要 `0.0.0.0`）

**入站调用（你发请求）**
- [ ] POST 到 `{site_origin}/api/omnibot/qq/webhook`
- [ ] `post_type` 固定 `"message"`
- [ ] `message_type` 只能是 `"group"` / `"private"`
- [ ] 群消息带 `group_id`，所有消息带 `user_id`
- [ ] **`raw_message` 带 CQ 码**（尤其 `[CQ:at,qq=数字]`）
- [ ] 密钥非空时带 `_secret`
- [ ] QQ 官方只转发平台真实 At，手打 CQ 码必须中和
- [ ] 读取响应体 `reply` 并回发到 QQ
- [ ] `status` 非 `reply` 时不要发消息

**会话映射（出站用）**
- [ ] 从①入站消息里记录 `group_id → 群会话`
- [ ] 从①入站消息里记录 `user_id → 私聊会话`
- [ ] 未命中映射时返回 404 并提示「先发一条消息」

---

## 9. 常见坑对照表

| 现象 | 原因 | 修复 |
|---|---|---|
| 转账永远不触发 | `raw_message` 是纯文本，没有 `[CQ:at,qq=...]` | 用协议端原始消息段重建 CQ 码 |
| `@某人 你好 +100` 不触发转账 | CQ 码与 `+` 之间夹了文字，正则要求只允许空白 | OmniBot 固有行为，网关不要改写 |
| 指令全部 `ignored_no_prefix` | `raw_message` 没以 `/` 开头 | 检查消息是否被 trim / 改写 |
| 全部 `ignored_chat_not_allowed` | `qq_allowed_groups` 为空 | 填群号，注意是字符串严格比较 |
| 私聊正常、群全部失败 | 白名单没配群号 | 同上 |
| 401 / 403 | 两边 `qq_bot_secret` 不一致 | 统一成一个值 |
| 测试连接超时 | 网关只监听 `127.0.0.1`，OmniBot 在容器里 | 监听 `0.0.0.0` 或用 `host.docker.internal` |
| 发请求 404 | `qq_bot_api_base` 填成了 `http://host:port/api` | 只填到端口，不带 `/api` |
| 回复发两遍 | 既读了 `reply` 又做了主动推送 | QQ 只读 `reply`，关掉主动推送 |
| 主动推送 404 | 该会话没发过消息，映射为空 | 先让用户在目标会话发一条 |
| OmniBot 无响应 | 数据库未初始化 | 返回 `{"status":"not_initialized"}` + 503 |

---

## 10. 源码索引

| 内容 | 文件:行 |
|---|---|
| webhook 路由 | `routes/api.php:6` |
| 入站鉴权 + 处理入口 | `src/Controllers/OmniBotController.php:24-44` |
| OneBot 事件解析 | `src/Controllers/OmniBotController.php:89-105` |
| 命令处理主流程 | `src/Controllers/OmniBotController.php:194-307` |
| 群白名单校验 | `src/Controllers/OmniBotController.php:320-331` |
| 转账入口与校验 | `src/Controllers/OmniBotController.php:347-361` |
| 响应构造（reply JSON） | `src/Controllers/OmniBotController.php:363-406` |
| 调试日志 | `src/Controllers/OmniBotController.php:485-492` |
| 出站 HTTP 客户端 | `src/OmniBot.php:99-150` |
| 连通性检查 | `src/OmniBot.php:17-30` |
| 命令定义表 | `src/Services/CommandRegistry.php:279-538` |
| 转账 CQ 码正则 | `src/Services/CommandRegistry.php:532-535` |
| 命令 token 规范化 | `src/Services/CommandRegistry.php:677-689` |
| 转账金额/绑定的二次解析 | `src/Handlers/TransferHandler.php:15-30` |
| 后台「测试连接」 | `src/Filament/Pages/OmniBotManagement.php:798-816` |
| 配置项默认值 | `src/Services/DatabaseBootstrapper.php:310-311` |
