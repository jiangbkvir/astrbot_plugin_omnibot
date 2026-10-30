"""AstrBot <-> NexusPHP OmniBot QQ 网关。

本插件只负责传输，不实现签到、绑定、转账等业务。业务仍由 OmniBot 处理。

双向契约：

1. QQ -> AstrBot -> OmniBot
   ``POST {omnibot_site_url}/api/omnibot/qq/webhook``
   请求体是 OneBot v11 消息事件，``raw_message`` 使用 CQ 码。

2. OmniBot -> 本插件 -> QQ
   ``GET  {gateway}/api/check``
   ``POST {gateway}/api/send_group_msg``
   ``POST {gateway}/api/send_private_msg``
   请求头使用 ``X-API-Key`` 鉴权。

NapCat、LLOneBot、Lagrange 通过 AstrBot 的 ``aiocqhttp`` 适配器提供
OneBot v11 数字 QQ 消息；QQ 官方平台（``qq_official``）只有 openid。网关可按
``message_parser`` 自动或强制选择解析方式，只把平台真实消息解析为 CQ 码，
不会把用户手打的 CQ 码当成真实消息，也不实现任何业务命令。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from aiohttp import ClientError, ClientTimeout, web
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star

MAX_SESSION_CACHE = 2000
MAX_PRINT_CHARS = 2000
DEFAULT_GATEWAY_PORT = 8080
MAX_BODY_BYTES = 1024 * 1024
MIN_SECRET_BYTES = 16
MARKDOWN_MAX_CHARS = 3500
GATEWAY_HANDLER_PRIORITY = 10000
QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_OPENAPI_BASE = "https://api.bot.qq.com"
DEFAULT_PANEL_SYNC_INTERVAL = 300
DEFAULT_PANEL_REMARK = "OmniBot 自动同步指令面板"
MAX_PANEL_ITEMS = 20
PANEL_SYNC_INITIAL_DELAY = 3
MESSAGE_PARSER_AUTO = "auto"
MESSAGE_PARSER_ONEBOT = "onebot"
MESSAGE_PARSER_QQ_OFFICIAL = "qq_official"
ONEBOT_PLATFORM_NAMES = frozenset(
    {"aiocqhttp", "onebot", "onebot_v11", "napcat", "llonebot", "lagrange"}
)
QQ_OFFICIAL_PLATFORM_NAMES = frozenset(
    {"qq_official", "qq_official_webhook", "qqofficial_webhook"}
)
MESSAGE_PARSER_ALIASES = {
    MESSAGE_PARSER_AUTO: MESSAGE_PARSER_AUTO,
    MESSAGE_PARSER_ONEBOT: MESSAGE_PARSER_ONEBOT,
    "onebot_v11": MESSAGE_PARSER_ONEBOT,
    "aiocqhttp": MESSAGE_PARSER_ONEBOT,
    "napcat": MESSAGE_PARSER_ONEBOT,
    "llonebot": MESSAGE_PARSER_ONEBOT,
    "lagrange": MESSAGE_PARSER_ONEBOT,
    MESSAGE_PARSER_QQ_OFFICIAL: MESSAGE_PARSER_QQ_OFFICIAL,
    "official": MESSAGE_PARSER_QQ_OFFICIAL,
    "qqofficial": MESSAGE_PARSER_QQ_OFFICIAL,
    "qq_official_webhook": MESSAGE_PARSER_QQ_OFFICIAL,
}
QQ_ADAPTER_TYPES = (
    filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.QQOFFICIAL
)
_QQ_OFFICIAL_WEBHOOK_ADAPTER_TYPE = getattr(
    filter.PlatformAdapterType,
    "QQOFFICIAL_WEBHOOK",
    None,
)
if _QQ_OFFICIAL_WEBHOOK_ADAPTER_TYPE is not None:
    QQ_ADAPTER_TYPES |= _QQ_OFFICIAL_WEBHOOK_ADAPTER_TYPE
DEFAULT_PANEL_ITEMS: tuple[dict[str, Any], ...] = (
    {"type": "command", "name": "help", "desc": "查看帮助信息", "only_admin": False},
    {"type": "command", "name": "bind", "desc": "绑定站点 /bind 密钥", "only_admin": False},
    {"type": "command", "name": "unbind", "desc": "解绑当前平台账号", "only_admin": False},
    {"type": "command", "name": "sign", "desc": "执行每日签到", "only_admin": False},
    {"type": "command", "name": "me", "desc": "查看站点账号信息", "only_admin": False},
    {"type": "command", "name": "lottery", "desc": "抽奖 /lottery [次数]", "only_admin": False},
    {"type": "command", "name": "pool", "desc": "查看当前奖池", "only_admin": False},
    {"type": "command", "name": "guess", "desc": "猜大小 /guess 大|小 金额", "only_admin": False},
    {"type": "command", "name": "fl", "desc": "固定抽奖 /fl 参与码", "only_admin": False},
    {"type": "command", "name": "rp", "desc": "发红包 /rp j|p 10 100000", "only_admin": False},
    {"type": "command", "name": "grab", "desc": "抢全站及QQ群红包", "only_admin": False},
)

PANEL_COMMAND_DESC_HINTS: dict[str, str] = {
    "bind": "绑定站点 /bind 密钥",
    "lottery": "抽奖 /lottery [次数]",
    "guess": "猜大小 /guess 大|小 金额",
    "fl": "固定抽奖 /fl 参与码",
    "rp": "发红包 /rp j|p 10 100000",
    "grab": "抢全站及QQ群红包",
}

_CQ_PREFIX_RE = re.compile(r"\[\s*cq\s*:", re.IGNORECASE)


class QqOpenApiError(RuntimeError):
    """QQ OpenAPI 返回的业务错误。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _as_str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        key = _as_str(key).strip()
        item = _as_str(item).strip()
        if key and item:
            result[key] = item
    return result


def _qq_text_width(text: str) -> int:
    """按 QQ 面板的字符限制计算宽度，中文等全角字符占 2。"""
    return sum(1 if ord(char) < 128 else 2 for char in text)


def _truncate_qq_text(text: str, max_width: int) -> str:
    """截断到 QQ 面板允许的显示宽度，避免 30013 超出数量限制。"""
    width = 0
    result: list[str] = []
    for char in text:
        char_width = _qq_text_width(char)
        if width + char_width > max_width:
            break
        result.append(char)
        width += char_width
    return "".join(result).rstrip()


def _to_int_if_possible(value: Any) -> int | str:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = _as_str(value).strip()
    return int(text) if text.isdigit() else text


def _neutralize_cq_codes(text: str) -> str:
    """把用户输入里的 CQ 码改成不可被 OmniBot 识别的文本。

    OneBot 平台仍使用协议端提供的原始消息段，不受这里影响。
    QQ 官方平台没有 CQ 码，用户手打的 ``[CQ:at,qq=...]`` 必须被中和，
    否则可能被下游误认为平台真实 At 段。
    """
    return _CQ_PREFIX_RE.sub("［CQ:", text)


_OMNIBOT_RULE_RE = re.compile(r"^[\s─\-=_*]{4,}$")
_OMNIBOT_DECORATION_RE = re.compile(r"^[\s─\-=_*·•]+$")
_OMNIBOT_FIELD_RE = re.compile(
    r"^[ \t]*([^：:]{1,20})[ \t]*[：:][ \t]*(.+?)[ \t]*$"
)
_OMNIBOT_BULLET_RE = re.compile(r"^[ \t]*[•·▪◦\-][ \t]*(.+?)[ \t]*$")
_OMNIBOT_TABLE_KEY_RE = re.compile(r"[()（）\[\]【】。！？；;]")
_OMNIBOT_TITLE_EMOJI_RE = re.compile(
    r"^[ \t]*[📖✅❌👤💰📅🏆🎫📤📥💸💵🎲🎰📊📭💡👥📌📣⏰🏆][^《》\[\]【】]{0,30}"
    r"[【\[][^】\]]{1,30}[】\]][ \t]*$"
)
_OMNIBOT_TITLE_PLAIN_RE = re.compile(
    r"^[ \t]*[📖✅❌👤🎲🎰📊][^：:]{0,30}[：:][ \t]*(.+?)[ \t]*$"
)
_OMNIBOT_COMMAND_RE = re.compile(r"^[ \t]*(/[^\s]{1,24}|@[^\s]{1,24})(?:[ \t]+.*)?$")


def _is_omnibot_rule_line(line: str) -> bool:
    """OmniBot 模板用 ──── 或 ---- 画分隔线。"""
    return bool(_OMNIBOT_RULE_RE.match(line))


def _is_omnibot_field_line(line: str) -> bool:
    """形如 用户名：foo 的键值行。"""
    return bool(_OMNIBOT_FIELD_RE.match(line))


def _is_omnibot_bullet_line(line: str) -> bool:
    return bool(_OMNIBOT_BULLET_RE.match(line))


def _is_omnibot_table_key(key: str) -> bool:
    """只有短字段名才适合做成表格，长句里的冒号应保留为正文。"""
    key = _strip_emoji_prefix(key)
    if not key or _qq_text_width(key) > 20:
        return False
    return not bool(_OMNIBOT_TABLE_KEY_RE.search(key))


_EMOJI_PREFIX_RE = re.compile(
    r"^[ \t]*[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF]+[ \t]*"
)
def _strip_emoji_prefix(text: str) -> str:
    """去掉字段名里的 emoji 前缀。

    表格第一列本身就是字段名，emoji 只是装饰；去掉后列宽更稳定，
    QQ 的 markdown 表格也更容易对齐。
    """
    return _EMOJI_PREFIX_RE.sub("", text, count=1).strip()


def _escape_md_table_cell(text: str) -> str:
    """表格单元格内的竖线必须按 GFM 规则转义，否则会被当成列分隔。"""
    return text.replace("|", "\\|")


_MARKDOWN_BLOCK_RE = re.compile(
    r"(?m)^(?:#{1,6}[ \t]+\S"
    r"|[ \t]*\|[^\n]*\|[ \t]*$"
    r"|[ \t]*[-*+][ \t]+\S"
    r"|[ \t]*\d+\.[ \t]+\S"
    r"|[ \t]*>[ \t]+\S"
    r"|[ \t]*```)"
)
_MARKDOWN_INLINE_RE = re.compile(
    r"\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\([^)\n]+\)"
)


def looks_like_markdown(text: str) -> bool:
    """判断 OmniBot 是否已经直接返回了 Markdown。

    后台模板一旦改成 Markdown，插件不能再按纯文本模板拆一遍，否则会把
    现成的表格和标题打散，所以这里先做透传判断。
    """
    return bool(
        _MARKDOWN_BLOCK_RE.search(text) or _MARKDOWN_INLINE_RE.search(text)
    )


def _split_omnibot_title(line: str) -> str | None:
    """取出一行标题的纯文本部分，去掉方括号装饰。"""
    if _OMNIBOT_TITLE_EMOJI_RE.match(line):
        # 形如 `👤 [用户信息查询]`：去掉 emoji 前缀和方括号，只留标题本体。
        inner = re.sub(r"^[ \t]*\S+[ \t]*", "", line).strip()
        return re.sub(r"^[【\[]|[】\]]$", "", inner).strip() or None
    plain = _OMNIBOT_TITLE_PLAIN_RE.match(line)
    if plain:
        return plain.group(1).strip() or None
    return None


class _MdBlock:
    """转换过程中的一个语义块。"""

    __slots__ = ("title", "fields", "bullets", "notes", "commands")

    def __init__(self) -> None:
        self.title: str | None = None
        self.fields: list[tuple[str, str]] = []
        self.bullets: list[str] = []
        self.notes: list[str] = []
        self.commands: list[str] = []

    def has_content(self) -> bool:
        return bool(
            self.title or self.fields or self.bullets or self.notes or self.commands
        )

    def only_has_title(self) -> bool:
        return bool(
            self.title and not self.fields and not self.bullets and not self.notes
            and not self.commands
        )


def _split_omnibot_command(command: str) -> tuple[str, str]:
    """把 `/bind [密钥] 绑定站点账号` 拆成指令列和说明列。

    指令列可能同时含主命令与别名（`/unbind /解绑`），别名同样以 `/` 开头，
    所以要一直吃到第一个非命令词为止。
    """
    tokens = command.split()
    names: list[str] = []
    index = 0
    while index < len(tokens) and _OMNIBOT_COMMAND_RE.match(tokens[index]):
        names.append(tokens[index])
        index += 1
    if not names:
        return command, ""
    return " ".join(names), " ".join(tokens[index:]).strip()


def _parse_omnibot_blocks(lines: list[str]) -> list[_MdBlock]:
    blocks: list[_MdBlock] = []
    current: _MdBlock | None = None
    pending_title: str | None = None

    def flush() -> None:
        nonlocal current, pending_title
        if current is not None and current.only_has_title():
            pending_title = current.title
            current = None
            return
        if current is not None and current.has_content():
            blocks.append(current)
        current = None

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            # 空行：字段/列表块视为结束，纯文本块保留（可能是多段说明）。
            if current is not None and (current.fields or current.bullets):
                flush()
            continue

        if _is_omnibot_rule_line(line):
            # 分隔线：已有内容的块先收尾，开始新的一段。
            flush()
            continue

        if _OMNIBOT_DECORATION_RE.match(line):
            continue

        if current is None:
            current = _MdBlock()
            if pending_title:
                current.title = pending_title
                pending_title = None

        bullet = _OMNIBOT_BULLET_RE.match(line)
        if bullet:
            current.bullets.append(bullet.group(1).strip())
            continue

        field = _OMNIBOT_FIELD_RE.match(line)
        if field:
            key = _strip_emoji_prefix(field.group(1))
            value = field.group(2).strip()
            if not _is_omnibot_table_key(key):
                field = None
            else:
                # `连续：7 天 | 累计：30 天` 这种一行多字段拆成多行，
                # 表格里就不会出现需要转义的竖线。
                parts = [part.strip() for part in value.split(" | ")]
                if len(parts) > 1:
                    for part in parts:
                        sub = _OMNIBOT_FIELD_RE.match(part)
                        if sub:
                            current.fields.append(
                                (
                                    _strip_emoji_prefix(sub.group(1)),
                                    sub.group(2).strip(),
                                )
                            )
                        else:
                            current.fields.append((key, part))
                else:
                    current.fields.append((key, value))
                continue

        text = line.strip()

        # 命令帮助行：`/help /帮助 查看帮助信息`。
        if _OMNIBOT_COMMAND_RE.match(text):
            current.commands.append(text)
            continue

        # 块内第一条文本行是标题，其余归为说明文字。
        if (
            current.title is None
            and not current.notes
            and not current.fields
            and not current.bullets
            and not current.commands
        ):
            current.title = _split_omnibot_title(text) or _strip_emoji_prefix(text)
        else:
            current.notes.append(text)

    flush()
    if pending_title:
        block = _MdBlock()
        block.title = pending_title
        blocks.append(block)
    return blocks


def omnibot_text_to_markdown(
    text: str,
    max_rows: int = 40,
    lottery_max_rows: int = 5,
) -> str:
    """把 OmniBot 的纯文本模板转成 Markdown。

    OmniBot 的回复模板用 `────` 分段、`键：值` 罗列数据，QQ 原生文本下没有
    任何排版。这里按语义重排成：标题、Markdown 表格、列表，让 QQ 官方能走
    原生 markdown 或 t2i 渲染出可读的排版。
    """
    if not text or not text.strip():
        return text

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    # 后台模板如果已经直接输出 Markdown，原样透传，避免把现成表格打散。
    if looks_like_markdown(normalized):
        return normalized
    # 单句状态/错误/提示没有结构化内容，任何标题或表格化都会更差。
    if "\n" not in normalized:
        return normalized

    lines = normalized.split("\n")
    blocks = _parse_omnibot_blocks(lines)
    if not blocks:
        return text.strip()

    # 只有一条内容极少的块时，不要硬套标题，保持原样更像人话。
    if len(blocks) == 1 and not blocks[0].fields and not blocks[0].commands:
        single = blocks[0]
        if single.bullets:
            return "\n".join(f"- {b}" for b in single.bullets)
        if single.notes and not single.title:
            return "\n".join(single.notes)
        if single.title and len(single.title) <= 12 and not single.notes:
            return single.title

    rendered: list[str] = []
    for index, block in enumerate(blocks):
        is_lottery_summary = bool(
            block.title and "中奖汇总" in block.title
        )
        field_limit = (
            max(0, lottery_max_rows) if is_lottery_summary else max_rows
        )
        # 第一个块的标题作为整条消息的主标题，其余按需降级为普通文字，
        # 避免满屏 ### 反而失去层级感。
        if block.title:
            title = block.title.strip().rstrip("：:")
            structured = bool(
                block.commands
                or block.bullets
                or len(block.fields) >= 4
            )
            if structured and _qq_text_width(title) <= 40:
                rendered.append(f"## {title}" if index == 0 else f"### {title}")
            else:
                rendered.append(f"**{title}**")
        if block.commands:
            # 命令列表用表格而不是代码块：本地 t2i 的等宽字体回退到
            # DejaVuSansMono，它没有 CJK 字形，中文会整片变成方框。
            if len(block.commands) == 1:
                name, desc = _split_omnibot_command(block.commands[0])
                rendered.append(f"- `{name}` {desc}".rstrip())
            else:
                rendered.append("| 指令 | 说明 |")
                rendered.append("| --- | --- |")
                for command in block.commands[:max_rows]:
                    name, desc = _split_omnibot_command(command)
                    rendered.append(
                        f"| `{_escape_md_table_cell(name)}` "
                        f"| {_escape_md_table_cell(desc)} |"
                    )
                if len(block.commands) > max_rows:
                    rendered.append(
                        f"| … | 其余 {len(block.commands) - max_rows} 条已省略 |"
                    )
        if block.fields:
            use_table = len(block.fields) >= 4 and all(
                _qq_text_width(value) <= 60 for _, value in block.fields
            )
            shown_fields = block.fields[:field_limit]
            if use_table and shown_fields:
                rendered.append("| 项目 | 值 |")
                rendered.append("| --- | --- |")
                for key, value in shown_fields:
                    rendered.append(
                        f"| {_escape_md_table_cell(key)} "
                        f"| {_escape_md_table_cell(value)} |"
                    )
            elif shown_fields:
                rendered.extend(
                    f"- **{key}**：{value}"
                    for key, value in shown_fields
                )

            folded_count = len(block.fields) - len(shown_fields)
            if folded_count:
                if is_lottery_summary:
                    prefix = (
                        f"共 {folded_count} 项中奖记录已折叠"
                        if not shown_fields
                        else f"其余 {folded_count} 项中奖记录已折叠"
                    )
                    rendered.append(
                        f"> {prefix}，完整记录请查看站内信。"
                    )
                else:
                    rendered.append(f"- … 其余 {folded_count} 项已省略")
        if block.bullets:
            rendered.extend(f"- {bullet}" for bullet in block.bullets[:max_rows])
            if len(block.bullets) > max_rows:
                rendered.append(f"- … 其余 {len(block.bullets) - max_rows} 项已省略")
        if block.notes:
            if block.commands or block.fields or block.bullets:
                rendered.append("")
            rendered.extend(block.notes)
        rendered.append("")

    result = "\n".join(rendered).strip()
    return result or text.strip()


class OmniBotGateway(Star):
    """只做 AstrBot 与 OmniBot 之间的双向消息网关。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}

        # 直接保存 AstrBot 的 MessageSession。QO 群会话的 session_id 是
        # group_openid，私聊会话的 session_id 是 user_openid；不能自行猜 UMO。
        self._session_by_group: dict[str, Any] = {}
        self._session_by_user: dict[str, Any] = {}
        self._session_seen_at: dict[str, float] = {}

        self._client: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._gateway_started = False
        self._gateway_lock: asyncio.Lock | None = None
        self._gateway_error_logged = False

        self._panel_sync_task: asyncio.Task | None = None
        self._panel_sync_stop: asyncio.Event | None = None
        self._qq_token_cache: dict[str, tuple[str, float]] = {}
        self._qq_panel_command_names: set[str] = set()
        self._panel_fallback_logged = False

        self._guess_watches: dict[int, dict[str, Any]] = {}
        self._guess_watch_task: asyncio.Task | None = None
        self._guess_watch_wakeup = asyncio.Event()

        if self.outbound_gateway_enabled:
            try:
                asyncio.get_running_loop().create_task(self._ensure_gateway_started())
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return _as_bool(self.config.get("enabled"), True)

    @property
    def message_parser(self) -> str:
        value = (
            _as_str(self.config.get("message_parser"), MESSAGE_PARSER_AUTO)
            .strip()
            .lower()
            .replace("-", "_")
        )
        return MESSAGE_PARSER_ALIASES.get(value, MESSAGE_PARSER_AUTO)

    @property
    def outbound_gateway_enabled(self) -> bool:
        return _as_bool(self.config.get("outbound_gateway_enabled"), False)

    @property
    def gateway_host(self) -> str:
        return (
            _as_str(self.config.get("gateway_host"), "127.0.0.1").strip() or "127.0.0.1"
        )

    @property
    def gateway_port(self) -> int:
        port = _as_int(self.config.get("gateway_port"), DEFAULT_GATEWAY_PORT)
        return port if 1 <= port <= 65535 else DEFAULT_GATEWAY_PORT

    @property
    def secret(self) -> str:
        return _as_str(self.config.get("qq_bot_secret"), "")

    @property
    def site_url(self) -> str:
        return _as_str(self.config.get("omnibot_site_url"), "").strip().rstrip("/")

    @property
    def webhook_url(self) -> str:
        return f"{self.site_url}/api/omnibot/qq/webhook"

    @property
    def allowed_groups(self) -> list[str]:
        return _as_str_list(self.config.get("allowed_groups"))

    @property
    def qq_group_id_map(self) -> dict[str, str]:
        return _as_str_map(self.config.get("qq_group_id_map"))

    @property
    def forward_private(self) -> bool:
        return _as_bool(self.config.get("forward_private"), True)

    @property
    def timeout(self) -> int:
        return max(1, _as_int(self.config.get("request_timeout"), 10))

    @property
    def stop_event_after_forward(self) -> bool:
        return _as_bool(self.config.get("stop_event_after_forward"), True)

    @property
    def reply_from_webhook_json(self) -> bool:
        return _as_bool(self.config.get("reply_from_webhook_json"), True)

    @property
    def reply_prefix(self) -> str:
        return _as_str(self.config.get("reply_prefix"), "")

    @property
    def reply_markdown_enabled(self) -> bool:
        return _as_bool(self.config.get("reply_markdown_enabled"), True)

    @property
    def reply_markdown_platforms(self) -> list[str]:
        configured = _as_str_list(self.config.get("reply_markdown_platforms"))
        return configured or ["qq_official"]

    @property
    def reply_markdown_max_rows(self) -> int:
        value = _as_int(self.config.get("reply_markdown_max_rows"), 40)
        return max(1, value)

    @property
    def reply_markdown_lottery_max_rows(self) -> int:
        value = _as_int(self.config.get("reply_markdown_lottery_max_rows"), 5)
        return max(0, min(value, self.reply_markdown_max_rows))

    @property
    def log_all_messages(self) -> bool:
        return _as_bool(self.config.get("log_all_messages"), True)

    @property
    def guess_result_watch_enabled(self) -> bool:
        return _as_bool(self.config.get("guess_result_watch_enabled"), True)

    @property
    def guess_result_watch_retry_seconds(self) -> int:
        value = _as_int(self.config.get("guess_result_watch_retry_seconds"), 10)
        return max(3, min(value, 60))

    @property
    def forward_to_omnibot(self) -> bool:
        return _as_bool(self.config.get("forward_to_omnibot"), True)

    @property
    def log_message_chain(self) -> bool:
        return _as_bool(self.config.get("log_message_chain"), True)

    @property
    def print_chars(self) -> int:
        value = _as_int(self.config.get("max_print_chars"), MAX_PRINT_CHARS)
        return value if value > 0 else MAX_PRINT_CHARS

    @property
    def command_panel_sync_enabled(self) -> bool:
        return _as_bool(self.config.get("command_panel_sync_enabled"), True)

    @property
    def command_panel_fallback_enabled(self) -> bool:
        return _as_bool(self.config.get("command_panel_fallback_enabled"), True)

    @property
    def command_panel_sync_interval(self) -> int:
        value = _as_int(
            self.config.get("command_panel_sync_interval"),
            DEFAULT_PANEL_SYNC_INTERVAL,
        )
        return max(30, value)

    @property
    def command_panel_target_type(self) -> str:
        value = _as_str(self.config.get("command_panel_target_type"), "all").strip()
        return value if value in {"all", "specific"} else "all"

    @property
    def command_panel_group_ids(self) -> list[str]:
        return _as_str_list(self.config.get("command_panel_group_ids"))

    @property
    def command_panel_remark(self) -> str:
        value = _as_str(
            self.config.get("command_panel_remark"), DEFAULT_PANEL_REMARK
        ).strip()
        return (value or DEFAULT_PANEL_REMARK)[:255]

    @property
    def command_panel_max_items(self) -> int:
        value = _as_int(self.config.get("command_panel_max_items"), MAX_PANEL_ITEMS)
        return max(1, min(MAX_PANEL_ITEMS, value))

    @property
    def command_panel_platform_id(self) -> str:
        return _as_str(self.config.get("command_panel_platform_id"), "").strip()

    @property
    def gateway_base_url(self) -> str:
        host = self.gateway_host
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        return f"http://{host}:{self.gateway_port}"

    def _to_omnibot_group_id(self, group_id: str) -> str:
        # aiocqhttp 等平台已经提供数字群号时直接透传，只有 QQ 官方的
        # group_openid 才需要按配置映射成 OmniBot 使用的数字群号。
        if group_id.isdigit():
            return group_id
        return self.qq_group_id_map.get(group_id, group_id)

    def _message_parser_for_event(self, event: AstrMessageEvent) -> str:
        """返回当前消息应使用的解析器。

        NapCat、LLOneBot、Lagrange 在 AstrBot 中统一由 ``aiocqhttp``
        （OneBot v11）适配器接入，因此共用 ``onebot`` 解析分支。
        """
        configured = self.message_parser
        if configured != MESSAGE_PARSER_AUTO:
            return configured

        platform_name = _as_str(event.get_platform_name()).strip().lower()
        if platform_name in QQ_OFFICIAL_PLATFORM_NAMES:
            return MESSAGE_PARSER_QQ_OFFICIAL
        if platform_name in ONEBOT_PLATFORM_NAMES:
            return MESSAGE_PARSER_ONEBOT

        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict) and raw.get("post_type") == "message":
            return MESSAGE_PARSER_ONEBOT

        return MESSAGE_PARSER_ONEBOT

    def _is_onebot_event(self, event: AstrMessageEvent) -> bool:
        return self._message_parser_for_event(event) == MESSAGE_PARSER_ONEBOT

    def _from_omnibot_group_id(self, group_id: str) -> str:
        for group_openid, group_number in self.qq_group_id_map.items():
            if group_number == group_id:
                return group_openid
        return group_id

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        if not self.enabled:
            self.logger.warning("OmniBot 网关插件已在配置中禁用。")
            return
        if self.outbound_gateway_enabled:
            await self._ensure_gateway_started()
        else:
            self.logger.info(
                "OmniBot 入站网关已启用；出站回调接口未开放，"
                "普通指令仍通过 webhook 响应中的 reply 回复。"
            )
        if not self.site_url:
            self.logger.error("未配置 omnibot_site_url，QQ 消息不会转发。")
        else:
            self.logger.info("OmniBot webhook 地址：%s", self.webhook_url)
        if not self.secret:
            self.logger.error("qq_bot_secret 为空：出站网关与入站转发均失败关闭。")
        elif len(self.secret.encode("utf-8")) < MIN_SECRET_BYTES:
            self.logger.warning("qq_bot_secret 少于 16 字节，建议使用更长的随机密钥。")
        self.logger.info("QQ 消息解析模式：%s", self.message_parser)
        self._start_command_panel_sync()

    async def terminate(self) -> None:
        await self._stop_guess_result_watch()
        await self._stop_command_panel_sync()
        await self._stop_gateway()
        await self._close_client()
        self._session_by_group.clear()
        self._session_by_user.clear()
        self._session_seen_at.clear()
        self.logger.info("OmniBot QQ 网关已卸载。")

    # ------------------------------------------------------------------
    # OmniBot -> 网关 -> QQ
    # ------------------------------------------------------------------
    async def _ensure_gateway_started(self) -> None:
        if (
            self._gateway_started
            or not self.enabled
            or not self.outbound_gateway_enabled
        ):
            return
        if self._gateway_lock is None:
            self._gateway_lock = asyncio.Lock()
        async with self._gateway_lock:
            if (
                self._gateway_started
                or not self.enabled
                or not self.outbound_gateway_enabled
            ):
                return

            app = web.Application(client_max_size=MAX_BODY_BYTES)
            app.router.add_get("/api/check", self._http_check)
            app.router.add_post("/api/send_group_msg", self._http_send_group_msg)
            app.router.add_post("/api/send_private_msg", self._http_send_private_msg)

            runner = web.AppRunner(app, access_log=None)
            try:
                await runner.setup()
                site = web.TCPSite(runner, self.gateway_host, self.gateway_port)
                await site.start()
            except OSError as exc:
                await runner.cleanup()
                if not self._gateway_error_logged:
                    self._gateway_error_logged = True
                    self.logger.error(
                        "网关启动失败（%s:%s）：%s。请修改 gateway_port，"
                        "并同步修改 OmniBot 的「QQ 服务地址」。",
                        self.gateway_host,
                        self.gateway_port,
                        exc,
                    )
                return
            except Exception as exc:  # noqa: BLE001
                await runner.cleanup()
                if not self._gateway_error_logged:
                    self._gateway_error_logged = True
                    self.logger.error("网关启动异常：%s", exc)
                return

            self._runner = runner
            self._gateway_started = True
            self._gateway_error_logged = False
            self.logger.info(
                "OmniBot 网关已启动：%s/api/{check,send_group_msg,send_private_msg}",
                self.gateway_base_url,
            )

    async def _stop_gateway(self) -> None:
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("关闭网关时出错：%s", exc)
        self._runner = None
        self._gateway_started = False

    def _authorized(self, request: web.Request) -> bool:
        expected = self.secret
        if not expected:
            return False
        provided = request.headers.get("X-API-Key", "")
        return hmac.compare_digest(
            provided.encode("utf-8", errors="replace"),
            expected.encode("utf-8"),
        )

    def _unauthorized(self) -> web.Response:
        self.logger.warning(
            "OmniBot 调用被拒绝：qq_bot_secret 为空或 X-API-Key 不匹配。"
        )
        return web.json_response({"error": "Unauthorized"}, status=401)

    async def _read_json(self, request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    async def _http_check(self, request: web.Request) -> web.Response:
        if not self.enabled:
            return web.json_response({"error": "plugin disabled"}, status=503)
        if not self._authorized(request):
            return self._unauthorized()
        return web.json_response(
            {
                "status": "ok",
                "plugin": "astrbot_plugin_omnibot",
                "groups": len(self._session_by_group),
                "users": len(self._session_by_user),
            }
        )

    async def _http_send_group_msg(self, request: web.Request) -> web.Response:
        if not self.enabled:
            return web.json_response({"error": "plugin disabled"}, status=503)
        if not self._authorized(request):
            return self._unauthorized()

        body = await self._read_json(request)
        group_id = _as_str(body.get("group_id")).strip()
        message = _as_str(body.get("message"))
        if not group_id or not message:
            return web.json_response(
                {"error": "group_id and message are required"}, status=400
            )

        session = self._session_by_group.get(group_id)
        if session is None:
            session = self._session_by_group.get(self._from_omnibot_group_id(group_id))
        if session is None:
            return web.json_response(
                {"error": f"unknown group: {group_id}"}, status=404
            )
        if not await self._push(session, message):
            return web.json_response({"error": "send failed"}, status=500)
        return web.json_response({"status": "ok", "data": {"message_id": 0}})

    async def _http_send_private_msg(self, request: web.Request) -> web.Response:
        if not self.enabled:
            return web.json_response({"error": "plugin disabled"}, status=503)
        if not self._authorized(request):
            return self._unauthorized()

        body = await self._read_json(request)
        user_id = _as_str(body.get("user_id")).strip()
        message = _as_str(body.get("message"))
        if not user_id or not message:
            return web.json_response(
                {"error": "user_id and message are required"}, status=400
            )

        session = self._session_by_user.get(user_id)
        if session is None:
            return web.json_response({"error": f"unknown user: {user_id}"}, status=404)
        if not await self._push(session, message):
            return web.json_response({"error": "send failed"}, status=500)
        return web.json_response({"status": "ok", "data": {"message_id": 0}})

    async def _push(self, session: Any, message: str) -> bool:
        chain = MessageChain()
        chain.message(message)
        try:
            await self.context.send_message(session, chain)
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.error("主动发送消息失败：%s", type(exc).__name__)
            return False

    # ------------------------------------------------------------------
    # OmniBot 指令清单 -> QQ 官方指令面板
    # ------------------------------------------------------------------
    def _start_command_panel_sync(self) -> None:
        if (
            not self.enabled
            or not self.command_panel_sync_enabled
            or not self.site_url
            or not self.secret
        ):
            return
        if self._panel_sync_task is not None and not self._panel_sync_task.done():
            return

        self._panel_sync_stop = asyncio.Event()
        self._panel_sync_task = asyncio.create_task(
            self._run_command_panel_sync(),
            name="omnibot-command-panel-sync",
        )
        self.logger.info(
            "QQ 指令面板同步任务已启动：interval=%ss",
            self.command_panel_sync_interval,
        )

    async def _stop_command_panel_sync(self) -> None:
        if self._panel_sync_stop is not None:
            self._panel_sync_stop.set()
        task = self._panel_sync_task
        self._panel_sync_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._panel_sync_stop = None

    async def _wait_panel_sync(self, seconds: float) -> bool:
        if self._panel_sync_stop is None:
            return True
        try:
            await asyncio.wait_for(self._panel_sync_stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run_command_panel_sync(self) -> None:
        if await self._wait_panel_sync(PANEL_SYNC_INITIAL_DELAY):
            return

        self.logger.info("QQ 指令面板开始首次同步。")
        while not (self._panel_sync_stop and self._panel_sync_stop.is_set()):
            try:
                await self._sync_qq_command_panel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.error("同步 QQ 指令面板失败：%s", exc)

            if await self._wait_panel_sync(self.command_panel_sync_interval):
                return

    def _find_qq_official_platform(self) -> Any | None:
        platform_id = self.command_panel_platform_id
        manager = getattr(self.context, "platform_manager", None)
        instances = getattr(manager, "platform_insts", None) or []
        for platform in instances:
            try:
                meta = platform.meta()
                if getattr(meta, "name", "") != "qq_official":
                    continue
                if platform_id and getattr(meta, "id", "") != platform_id:
                    continue
                return platform
            except Exception:
                continue
        return None

    async def _get_qq_access_token(self, platform: Any) -> str:
        app_id = _as_str(getattr(platform, "appid", "")).strip()
        app_secret = _as_str(getattr(platform, "secret", "")).strip()
        if not app_id or not app_secret:
            raise QqOpenApiError("AstrBot 的 QQ 官方平台缺少 appid 或 secret")

        now = time.time()
        cached = self._qq_token_cache.get(app_id)
        if cached and cached[1] > now + 60:
            return cached[0]

        async with self._ensure_client().post(
            QQ_TOKEN_URL,
            json={"appId": app_id, "clientSecret": app_secret},
            timeout=ClientTimeout(total=self.timeout),
        ) as response:
            text = await response.text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                data = {}
            if response.status >= 300 or not isinstance(data, dict):
                raise QqOpenApiError(
                    f"获取 QQ access_token 失败：HTTP {response.status}"
                )
            token = _as_str(data.get("access_token")).strip()
            if not token:
                code = _as_int(data.get("code"), 0) or None
                message = _as_str(data.get("message"), "响应中没有 access_token")
                raise QqOpenApiError(f"获取 QQ access_token 失败：{message}", code)
            expires_in = max(120, _as_int(data.get("expires_in"), 7200))
            self._qq_token_cache[app_id] = (token, now + expires_in - 60)
            return token

    async def _qq_api_request(
        self,
        token: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict:
        headers = {"Authorization": f"QQBot {token}"}
        last_error: QqOpenApiError | None = None

        for attempt in range(3):
            async with self._ensure_client().request(
                method,
                f"{QQ_OPENAPI_BASE}{path}",
                params=params,
                json=payload,
                headers=headers,
                timeout=ClientTimeout(total=self.timeout),
            ) as response:
                text = await response.text()
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    data = {}
                if response.status < 300:
                    return data if isinstance(data, dict) else {"data": data}

                code_value = data.get("code") if isinstance(data, dict) else None
                code = _as_int(code_value, 0) or None
                message = (
                    _as_str(data.get("message"), text[:500])
                    if isinstance(data, dict)
                    else text[:500]
                )
                last_error = QqOpenApiError(
                    f"QQ OpenAPI {method} {path} 失败：HTTP {response.status}"
                    f"{f' / {code}' if code else ''} {message}".strip(),
                    code,
                )
                # 40030009 表示面板正在处理上一笔操作，短暂等待后重试。
                if code == 40030009 and attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise last_error

        raise last_error or QqOpenApiError("QQ OpenAPI 请求失败")

    async def _fetch_command_panel_manifest(self) -> dict | None:
        if not self.site_url or not self.secret:
            return None
        try:
            async with self._ensure_client().get(
                f"{self.site_url}/api/omnibot/qq/commands",
                headers={"X-API-Key": self.secret},
                timeout=ClientTimeout(total=self.timeout),
            ) as response:
                text = await response.text()
                if response.status >= 300:
                    self.logger.error(
                        "读取 OmniBot 指令清单失败：HTTP %s %s",
                        response.status,
                        text[:500],
                    )
                    return self._fallback_command_panel_manifest()
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    self.logger.error("OmniBot 指令清单不是有效 JSON")
                    return self._fallback_command_panel_manifest()
                if not isinstance(data, dict):
                    return self._fallback_command_panel_manifest()
                self._panel_fallback_logged = False
                return data
        except asyncio.TimeoutError:
            self.logger.error("读取 OmniBot 指令清单超时（%ss）", self.timeout)
        except ClientError as exc:
            self.logger.error("读取 OmniBot 指令清单网络错误：%s", exc)
        return self._fallback_command_panel_manifest()

    def _fallback_command_panel_manifest(self) -> dict | None:
        if not self.command_panel_fallback_enabled:
            return None
        if not self._panel_fallback_logged:
            self.logger.warning(
                "OmniBot 指令清单不可用，QQ 面板暂时使用插件内置指令清单；"
                "请部署 /api/omnibot/qq/commands 以启用动态同步。"
            )
            self._panel_fallback_logged = True
        return {
            "status": "ok",
            "source": "astrbot_local_fallback",
            "items": [dict(item) for item in DEFAULT_PANEL_ITEMS],
        }

    def _normalize_panel_items(
        self,
        raw_items: Any,
        *,
        apply_desc_hints: bool = False,
    ) -> list[dict]:
        if not isinstance(raw_items, list):
            return []

        items: list[dict] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            if _as_str(raw.get("type"), "command") != "command":
                continue
            # QQ 指令面板点击 command 元素后，会把 name 原样填入输入框。
            # QQ OpenAPI 又会去掉 name 开头的 "/"，因此本地统一保存无斜杠
            # 形式用于幂等比较，并在入站时只对已知面板指令补回斜杠。
            name = _truncate_qq_text(
                _as_str(raw.get("name")).strip().lstrip("/"), 14
            )
            if not name:
                continue
            desc = _truncate_qq_text(
                re.sub(r"\s+", " ", _as_str(raw.get("desc"))).strip(), 30
            )
            # OmniBot 的设置项可能只返回“发红包”“/猜大小”这类短描述。
            # 对需要参数的常用交互指令，插件本地补成可直接照抄的用法。
            if apply_desc_hints:
                desc = _truncate_qq_text(
                    PANEL_COMMAND_DESC_HINTS.get(name.lower(), desc), 30
                )
            items.append(
                {
                    "type": "command",
                    "name": name,
                    "desc": desc,
                    "only_admin": _as_bool(raw.get("only_admin"), False),
                }
            )
            if len(items) >= self.command_panel_max_items:
                break
        return items

    def _panel_group_openids(self) -> list[str]:
        configured = self.command_panel_group_ids
        if not configured:
            configured = list(self.qq_group_id_map.keys())

        result: list[str] = []
        for group_id in configured:
            resolved = self._from_omnibot_group_id(group_id)
            if not resolved or resolved.isdigit():
                self.logger.warning(
                    "QQ 指令面板无法关联群 %s：缺少 group_openid 映射",
                    group_id,
                )
                continue
            if resolved not in result:
                result.append(resolved)
        return result

    @staticmethod
    def _panel_items_signature(items: list[dict]) -> str:
        return json.dumps(
            items, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    async def _list_qq_panels(self, token: str, remark: str) -> tuple[dict | None, int]:
        cursor = ""
        total = 0
        matched: dict | None = None
        while True:
            params: dict[str, Any] = {"scope": "group", "limit": 50}
            if cursor:
                params["cursor"] = cursor
            data = await self._qq_api_request(token, "GET", "/v2/panels", params=params)
            records = data.get("records")
            if not isinstance(records, list):
                records = []
            total += len(records)
            for record in records:
                if not isinstance(record, dict):
                    continue
                panel = record.get("panel")
                if not isinstance(panel, dict):
                    continue
                if _as_str(panel.get("remark")).strip() != remark:
                    continue
                if _as_str(record.get("scope"), "group") != "group":
                    continue
                matched = record
                break
            if matched is not None:
                break
            cursor = _as_str(data.get("next_cursor")).strip()
            if _as_bool(data.get("is_end"), False) or not cursor:
                break
        return matched, total

    async def _sync_group_targets(
        self,
        token: str,
        panel_id: str,
        current: list[str],
        desired: list[str],
    ) -> bool:
        current_set = set(current)
        desired_set = set(desired)
        to_add = sorted(desired_set - current_set)
        to_delete = sorted(current_set - desired_set)
        changed = False

        for op, values in (("add", to_add), ("del", to_delete)):
            for offset in range(0, len(values), 20):
                chunk = values[offset : offset + 20]
                if not chunk:
                    continue
                await self._qq_api_request(
                    token,
                    "PUT",
                    f"/v2/panels/{panel_id}/target",
                    payload={"op": op, "group_openids": chunk},
                )
                changed = True
        return changed

    async def _sync_qq_command_panel(self) -> bool:
        if (
            not self.enabled
            or not self.command_panel_sync_enabled
            or not self.site_url
            or not self.secret
        ):
            return False

        platform = self._find_qq_official_platform()
        if platform is None:
            self.logger.warning(
                "未找到已启用的 AstrBot QQ 官方平台，暂时无法同步指令面板。"
            )
            return False

        self.logger.debug("QQ 指令面板同步：开始读取 OmniBot 指令清单。")
        manifest = await self._fetch_command_panel_manifest()
        if manifest is None:
            return False
        items = self._normalize_panel_items(
            manifest.get("items"), apply_desc_hints=True
        )
        self.logger.debug("QQ 指令面板同步：已读取 %s 条指令。", len(items))
        self._qq_panel_command_names = {item["name"] for item in items}
        remark = self.command_panel_remark
        target_type = self.command_panel_target_type
        desired_groups: list[str] = []
        if target_type == "specific":
            desired_groups = self._panel_group_openids()
            if not desired_groups:
                self.logger.error(
                    "QQ 指令面板配置为 specific，但没有可用的 group_openid。"
                )
                return False

        self.logger.debug("QQ 指令面板同步：正在获取 QQ access_token。")
        token = await self._get_qq_access_token(platform)
        self.logger.debug("QQ 指令面板同步：access_token 已获取。")
        existing, total = await self._list_qq_panels(token, remark)
        self.logger.debug(
            "QQ 指令面板同步：已读取现有面板（total=%s，matched=%s）。",
            total,
            existing is not None,
        )
        if total >= 20 and existing is None:
            self.logger.error("QQ 指令面板数量已达 20 个，无法再创建 OmniBot 面板。")
            return False

        if (
            existing is not None
            and _as_str(existing.get("target_type"), "all") != target_type
        ):
            panel_id = _as_str(existing.get("panel_id")).strip()
            if panel_id:
                self.logger.warning(
                    "QQ 指令面板作用范围不一致，删除后重建：%s", panel_id
                )
                await self._qq_api_request(token, "DELETE", f"/v2/panels/{panel_id}")
            existing = None

        if existing is None:
            payload: dict[str, Any] = {
                "scope": "group",
                "target_type": target_type,
                "panel": {"items": items, "remark": remark},
            }
            if target_type == "specific":
                payload["group_openids"] = desired_groups
            data = await self._qq_api_request(
                token, "POST", "/v2/panels", payload=payload
            )
            panel_id = _as_str(data.get("panel_id")).strip()
            self.logger.info(
                "QQ 指令面板已创建：panel_id=%s commands=%s groups=%s",
                panel_id or "-",
                len(items),
                len(desired_groups),
            )
            return True

        panel_id = _as_str(existing.get("panel_id")).strip()
        if not panel_id:
            return False
        detail = await self._qq_api_request(token, "GET", f"/v2/panels/{panel_id}")
        current_panel = detail.get("panel")
        if not isinstance(current_panel, dict):
            current_panel = {}
        current_items = self._normalize_panel_items(current_panel.get("items"))
        panel_changed = (
            self._panel_items_signature(current_items)
            != self._panel_items_signature(items)
            or _as_str(current_panel.get("remark")).strip() != remark
        )

        if panel_changed:
            await self._qq_api_request(
                token,
                "PUT",
                f"/v2/panels/{panel_id}",
                payload={"panel": {"items": items, "remark": remark}},
            )

        targets_changed = False
        if target_type == "specific":
            current_groups = detail.get("group_openids")
            if not isinstance(current_groups, list):
                current_groups = []
            targets_changed = await self._sync_group_targets(
                token,
                panel_id,
                [_as_str(group_id) for group_id in current_groups],
                desired_groups,
            )

        if panel_changed or targets_changed:
            self.logger.info(
                "QQ 指令面板已更新：panel_id=%s commands=%s groups=%s",
                panel_id,
                len(items),
                len(desired_groups),
            )
        else:
            self.logger.debug(
                "QQ 指令面板无变化：panel_id=%s commands=%s", panel_id, len(items)
            )
        return panel_changed or targets_changed

    # ------------------------------------------------------------------
    # QQ -> 网关 -> OmniBot
    # ------------------------------------------------------------------
    # 网关必须先于 AstrBot 的聊天开关、AI 和其他群管插件拿到原始事件，
    # 否则 stop_event() 会让 OmniBot 指令根本没有机会转发。
    @filter.event_message_type(
        filter.EventMessageType.ALL,
        priority=GATEWAY_HANDLER_PRIORITY,
    )
    @filter.platform_adapter_type(
        QQ_ADAPTER_TYPES,
        priority=GATEWAY_HANDLER_PRIORITY,
    )
    async def on_qq_message(self, event: AstrMessageEvent):
        """转发 QQ 消息，不解析、不执行任何业务命令。"""
        if not self.enabled:
            return
        if event.get_message_type() not in (
            MessageType.GROUP_MESSAGE,
            MessageType.FRIEND_MESSAGE,
        ):
            return

        msg_obj = event.message_obj
        group_id = str(getattr(msg_obj, "group_id", "") or "")
        is_group = bool(group_id)
        sender_openid = event.get_sender_id()
        user_id = sender_openid
        omnibot_group_id = self._to_omnibot_group_id(group_id) if is_group else group_id

        # 打印优先于一切，即使转发被关闭或密钥缺失也照常输出。
        self._print_incoming(event, msg_obj, is_group, group_id, user_id)

        if not self.forward_to_omnibot:
            return
        if not self.site_url:
            return
        if not self.secret:
            self.logger.error("qq_bot_secret 为空，拒绝转发 QQ 消息。")
            event.stop_event()
            return

        allowed = self.allowed_groups
        if (
            is_group
            and allowed
            and group_id not in allowed
            and omnibot_group_id not in allowed
        ):
            return
        if not is_group and not self.forward_private:
            return
        if not sender_openid:
            return

        self._remember_session(group_id, sender_openid, event.session, is_group)
        raw_message = self._build_raw_message(event)
        if not raw_message:
            self.logger.debug(
                "跳过无消息内容的事件：group=%s user=%s", group_id, user_id
            )
            return

        self.logger.info(
            "OmniBot 网关命中：type=%s group=%s omnibot_group=%s user=%s",
            "group" if is_group else "private",
            group_id or "-",
            omnibot_group_id or "-",
            user_id,
        )
        payload = self._build_onebot_event(event, msg_obj, is_group, raw_message)
        reply_text, omnibot_meta = await self._post_to_omnibot(payload)
        self._register_guess_result_watch(omnibot_meta)
        # 一旦进入 OmniBot 转发流程，就由网关接管这条 QQ 消息。
        # 即使响应超时或鉴权失败，也不能回落到 AstrBot 的 LLM 并弹出模型错误。
        if self.stop_event_after_forward:
            event.stop_event()
        if reply_text is None:
            return
        self.logger.info(
            "OmniBot webhook 成功：type=%s group=%s user=%s has_reply=%s",
            "group" if is_group else "private",
            group_id or "-",
            user_id,
            bool(reply_text),
        )

        if reply_text:
            await self._send_reply(event, reply_text, is_group, user_id)

    def _print_incoming(
        self,
        event: AstrMessageEvent,
        msg_obj: Any,
        is_group: bool,
        group_id: str,
        user_id: str,
    ) -> None:
        """把收到的消息原样打到日志，只观测、不改变任何转发行为。"""
        if not self.log_all_messages:
            return

        raw = getattr(msg_obj, "raw_message", None)
        if isinstance(raw, dict):
            raw = raw.get("message") or raw.get("raw_message")
        raw_text = _as_str(raw)
        limit = self.print_chars
        raw_out = raw_text[:limit] + ("..." if len(raw_text) > limit else "")

        text = _as_str(event.message_str).strip()
        text_out = text[:limit] + ("..." if len(text) > limit else "")

        self.logger.info(
            "[消息打印] 类型=%s 平台=%s 解析器=%s 群ID=%s 发送者=%s(%s) 消息ID=%s",
            "群消息" if is_group else "私聊",
            event.get_platform_name() or "-",
            self._message_parser_for_event(event),
            group_id or "-",
            event.get_sender_name() or "-",
            user_id or "-",
            _as_str(getattr(msg_obj, "message_id", "")) or "-",
        )
        self.logger.info("[消息打印] 纯文本=%s", text_out or "(空)")
        self.logger.info("[消息打印] raw_message=%s", raw_out or "(空)")

        if self.log_message_chain:
            chain = list(getattr(msg_obj, "message", None) or [])
            if chain:
                rendered = ", ".join(self._render_segment(seg) for seg in chain)
                rendered = rendered[:limit] + ("..." if len(rendered) > limit else "")
                self.logger.info("[消息打印] 消息链=[%s]", rendered)

    @staticmethod
    def _render_segment(seg: Any) -> str:
        name = type(seg).__name__
        for attr in ("text", "qq", "id", "url", "file", "name"):
            value = getattr(seg, attr, None)
            if isinstance(value, (str, int)) and str(value) != "":
                return f"{name}({value})"
        return name

    def _build_raw_message(self, event: AstrMessageEvent) -> str:
        """从平台真实消息构建 raw_message，绝不信任用户手写的 CQ 码。"""
        raw = getattr(event.message_obj, "raw_message", None)
        parser = self._message_parser_for_event(event)

        # OneBot：优先使用协议端的消息段/原始 CQ 字符串。
        if parser == MESSAGE_PARSER_ONEBOT:
            onebot_text = ""
            if isinstance(raw, dict):
                message = raw.get("message")
                if isinstance(message, str) and message.strip():
                    onebot_text = message.strip()
                elif isinstance(message, list):
                    rebuilt = self._segments_to_cq(message)
                    if rebuilt:
                        onebot_text = rebuilt
                if not onebot_text:
                    raw_str = raw.get("raw_message")
                    if isinstance(raw_str, str) and raw_str.strip():
                        onebot_text = raw_str.strip()
            if not onebot_text:
                onebot_text = self._chain_to_cq(
                    getattr(event.message_obj, "message", None)
                )
            if not onebot_text:
                onebot_text = (event.message_str or "").strip()
            self_id = _as_str(event.get_self_id()).strip()
            if not self_id and isinstance(raw, dict):
                # 少数兼容层只把 self_id 放在原始 OneBot 事件里。
                self_id = _as_str(raw.get("self_id")).strip()
            return self._strip_onebot_self_mentions(
                onebot_text,
                self_id,
            )

        # QQ 官方：消息链中的 At 才是平台真实 mention。仅中和 Plain 文本里的 CQ 码。
        self_mention_ids = self._qq_official_self_mention_ids(event, raw)
        rebuilt = self._chain_to_cq(
            getattr(event.message_obj, "message", None),
            neutralize_plain=True,
            exclude_at_ids=self_mention_ids,
        )
        rebuilt = self._map_qq_official_mentions(rebuilt, raw)
        if not rebuilt:
            rebuilt = _neutralize_cq_codes((event.message_str or "").strip())
        return self._restore_qq_panel_command_slash(rebuilt)

    @staticmethod
    def _strip_onebot_self_mentions(text: str, self_id: str) -> str:
        """移除命令前的机器人自身 At，保留对其他群成员的 At。"""
        self_id = self_id.strip()
        if not text or not self_id:
            return text.strip()

        pattern = re.compile(
            rf"\[\s*cq\s*:\s*at\s*,[^\]]*?\bqq\s*=\s*{re.escape(self_id)}\b[^\]]*\]",
            re.IGNORECASE,
        )
        return pattern.sub("", text).strip()

    @staticmethod
    def _qq_official_self_mention_ids(
        event: AstrMessageEvent, raw: Any
    ) -> set[str]:
        """找出机器人的 mention ID，转发前移除 QQ 面板自动添加的 @机器人。"""
        ids = {_as_str(event.get_self_id()).strip()}
        mentions = getattr(raw, "mentions", None)
        if isinstance(mentions, (list, tuple)):
            for mention in mentions:
                if isinstance(mention, dict):
                    mention_id = _as_str(mention.get("id")).strip()
                    is_you = bool(mention.get("is_you"))
                else:
                    mention_id = _as_str(getattr(mention, "id", "")).strip()
                    is_you = bool(getattr(mention, "is_you", False))
                if mention_id and is_you:
                    ids.add(mention_id)
        return {mention_id for mention_id in ids if mention_id}

    def _restore_qq_panel_command_slash(self, text: str) -> str:
        """QQ 面板填入的指令名没有斜杠，仅对已同步的指令自动补回。"""
        text = text.strip()
        if not text or text.startswith("/"):
            return text
        name, separator, arguments = text.partition(" ")
        if name not in self._qq_panel_command_names:
            return text
        return f"/{name}{separator}{arguments}"

    def _map_qq_official_mentions(self, text: str, raw: Any) -> str:
        """把 QQ 官方真实 mention 转成可被 OmniBot 使用的 CQ At。"""
        mentions = getattr(raw, "mentions", None)
        if not isinstance(mentions, (list, tuple)):
            return text
        for mention in mentions:
            if isinstance(mention, dict):
                mention_id = _as_str(mention.get("id")).strip()
                is_you = bool(mention.get("is_you"))
            else:
                mention_id = _as_str(getattr(mention, "id", "")).strip()
                is_you = bool(getattr(mention, "is_you", False))
            if not mention_id or is_you:
                continue
            text = text.replace(f"<@{mention_id}>", f"[CQ:at,qq={mention_id}]")
            text = text.replace(f"<@!{mention_id}>", f"[CQ:at,qq={mention_id}]")
        return text

    @staticmethod
    def _segments_to_cq(segments: list) -> str:
        parts: list[str] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            data = seg.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            if seg_type == "text":
                parts.append(_as_str(data.get("text")))
            elif seg_type == "at":
                qq = _as_str(data.get("qq"))
                if qq:
                    parts.append(f"[CQ:at,qq={qq}]")
            elif seg_type == "face":
                face_id = _as_str(data.get("id"))
                if face_id:
                    parts.append(f"[CQ:face,id={face_id}]")
            elif seg_type == "image":
                file_ = _as_str(data.get("file") or data.get("url"))
                if file_:
                    parts.append(f"[CQ:image,file={file_}]")
            elif seg_type == "reply":
                reply_id = _as_str(data.get("id"))
                if reply_id:
                    parts.append(f"[CQ:reply,id={reply_id}]")
            elif seg_type:
                params = ",".join(f"{k}={v}" for k, v in data.items() if v is not None)
                parts.append(
                    f"[CQ:{seg_type},{params}]" if params else f"[CQ:{seg_type}]"
                )
        return "".join(parts).strip()

    @classmethod
    def _chain_to_cq(
        cls,
        chain: list | None,
        *,
        neutralize_plain: bool = False,
        exclude_at_ids: set[str] | None = None,
    ) -> str:
        if not chain:
            return ""
        excluded = exclude_at_ids or set()
        parts: list[str] = []
        for comp in chain:
            if isinstance(comp, Comp.Plain):
                text = _as_str(getattr(comp, "text", ""))
                parts.append(_neutralize_cq_codes(text) if neutralize_plain else text)
            elif isinstance(comp, Comp.At):
                qq = _as_str(getattr(comp, "qq", ""))
                if qq and qq not in excluded:
                    parts.append(f"[CQ:at,qq={qq}]")
            elif isinstance(comp, Comp.Face):
                face_id = _as_str(getattr(comp, "id", ""))
                if face_id:
                    parts.append(f"[CQ:face,id={face_id}]")
            elif isinstance(comp, Comp.Image):
                file_ = _as_str(getattr(comp, "file", "") or getattr(comp, "url", ""))
                if file_:
                    parts.append(f"[CQ:image,file={file_}]")
            elif isinstance(comp, Comp.Reply):
                reply_id = _as_str(getattr(comp, "id", ""))
                if reply_id:
                    parts.append(f"[CQ:reply,id={reply_id}]")
        return "".join(parts).strip()

    def _build_onebot_event(
        self,
        event: AstrMessageEvent,
        msg_obj: Any,
        is_group: bool,
        raw_message: str,
    ) -> dict:
        group_id = str(getattr(msg_obj, "group_id", "") or "")
        sender_openid = event.get_sender_id()
        user_id = sender_openid
        sender_name = event.get_sender_name()
        message_id = _as_str(getattr(msg_obj, "message_id", ""))
        parser = self._message_parser_for_event(event)

        payload: dict[str, Any] = {
            "post_type": "message",
            "message_type": "group" if is_group else "private",
            "user_id": _to_int_if_possible(user_id),
            "message_id": _to_int_if_possible(message_id) if message_id else 0,
            "message": raw_message,
            "raw_message": raw_message,
            "message_str": raw_message,
            "self_id": _to_int_if_possible(_as_str(event.get_self_id())) or 0,
            "time": int(time.time()),
            "sender": {
                "user_id": _to_int_if_possible(user_id),
                "nickname": sender_name or str(user_id),
                "card": sender_name,
            },
            "sub_type": "normal" if is_group else "friend",
        }
        omnibot_group_id = self._to_omnibot_group_id(group_id)
        if parser == MESSAGE_PARSER_QQ_OFFICIAL:
            payload["sender_openid"] = sender_openid
            if is_group:
                payload["group_id"] = _to_int_if_possible(omnibot_group_id)
                payload["group_openid"] = group_id
                payload["member_openid"] = sender_openid
            else:
                payload["user_openid"] = sender_openid
        elif is_group:
            # NapCat / LLOneBot / Lagrange 均使用 OneBot v11 数字群号。
            payload["group_id"] = _to_int_if_possible(omnibot_group_id)
        payload["_secret"] = self.secret
        return payload

    async def _post_to_omnibot(self, payload: dict) -> tuple[str | None, dict[str, Any]]:
        timeout = ClientTimeout(total=self.timeout)
        try:
            async with self._ensure_client().post(
                self.webhook_url, json=payload, timeout=timeout
            ) as resp:
                if resp.status >= 300:
                    self.logger.error("OmniBot webhook 返回 HTTP %s", resp.status)
                    return None, {}
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return "", {}
                if isinstance(data, dict):
                    status = _as_str(data.get("status")).strip()
                    if status:
                        self.logger.info("OmniBot webhook 状态：%s", status)
        except asyncio.TimeoutError:
            self.logger.error("请求 OmniBot webhook 超时（%ss）", self.timeout)
            return None, {}
        except ClientError as exc:
            self.logger.error("请求 OmniBot webhook 网络错误：%s", exc)
            return None, {}
        except Exception as exc:  # noqa: BLE001
            self.logger.error("请求 OmniBot webhook 出现未知错误：%s", exc)
            return None, {}

        if not isinstance(data, dict):
            return "", {}
        metadata = data.get("_omnibot")
        return (
            _as_str(data.get("reply")).strip()
            if self.reply_from_webhook_json
            else "",
            metadata if isinstance(metadata, dict) else {},
        )

    # ------------------------------------------------------------------
    # 猜大小开奖结果：只监听本次实际下注产生的活动目标
    # ------------------------------------------------------------------
    def _register_guess_result_watch(self, metadata: dict[str, Any]) -> None:
        if not self.guess_result_watch_enabled:
            return

        raw_watch = metadata.get("guess_result_watch")
        if not isinstance(raw_watch, dict):
            return

        target_id = _as_int(raw_watch.get("target_id"), 0)
        chat_type = _as_str(raw_watch.get("chat_type")).strip()
        chat_id = _as_str(raw_watch.get("chat_id")).strip()
        expires_at = self._parse_iso_timestamp(raw_watch.get("expires_at"))
        if (
            target_id <= 0
            or chat_type not in {"group", "private"}
            or not chat_id
            or expires_at is None
            or expires_at <= time.time()
        ):
            return

        next_check_at = self._parse_iso_timestamp(raw_watch.get("next_check_at"))
        if next_check_at is None or next_check_at <= 0:
            next_check_at = time.time()
        next_check_at = min(next_check_at, expires_at)

        existing = self._guess_watches.get(target_id)
        if existing is not None and existing.get("awaiting_ack"):
            next_check_at = min(next_check_at, time.time())

        self._guess_watches[target_id] = {
            "target_id": target_id,
            "chat_type": chat_type,
            "chat_id": chat_id,
            "chat_openid": _as_str(raw_watch.get("chat_openid")).strip() or None,
            "next_check_at": next_check_at,
            "expires_at": expires_at,
            "awaiting_ack": bool(existing and existing.get("awaiting_ack")),
        }
        self.logger.info(
            "猜大小开奖监听已登记：target=%s type=%s chat=%s next=%s expires=%s",
            target_id,
            chat_type,
            chat_id,
            self._format_timestamp(next_check_at),
            self._format_timestamp(expires_at),
        )

        self._guess_watch_wakeup.set()
        if self._guess_watch_task is None or self._guess_watch_task.done():
            self._guess_watch_task = asyncio.create_task(
                self._run_guess_result_watch_loop(),
                name="omnibot-guess-result-watch",
            )

    async def _stop_guess_result_watch(self) -> None:
        self._guess_watches.clear()
        self._guess_watch_wakeup.set()
        task = self._guess_watch_task
        self._guess_watch_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run_guess_result_watch_loop(self) -> None:
        try:
            while self.enabled and self.guess_result_watch_enabled and self._guess_watches:
                now = time.time()
                awaiting_ack_due = [
                    target_id
                    for target_id, watch in self._guess_watches.items()
                    if watch.get("awaiting_ack")
                    and float(watch.get("next_check_at", now)) <= now
                ]
                for target_id in awaiting_ack_due:
                    if await self._ack_guess_result(target_id, "sent"):
                        self._guess_watches.pop(target_id, None)
                    elif target_id in self._guess_watches:
                        self._guess_watches[target_id]["next_check_at"] = (
                            now + self.guess_result_watch_retry_seconds
                        )

                if not self._guess_watches:
                    break

                due_ids = [
                    target_id
                    for target_id, watch in self._guess_watches.items()
                    if not watch.get("awaiting_ack")
                    and (
                        float(watch.get("expires_at", 0)) <= now
                        or float(watch.get("next_check_at", now)) <= now
                    )
                ]
                if not due_ids:
                    self._prune_expired_guess_watches(now)
                    if not self._guess_watches:
                        break
                    next_due = min(
                        float(watch.get("next_check_at", now))
                        for watch in self._guess_watches.values()
                    )
                    delay = max(0.1, next_due - now)
                    try:
                        await asyncio.wait_for(
                            self._guess_watch_wakeup.wait(), timeout=delay
                        )
                    except asyncio.TimeoutError:
                        pass
                    self._guess_watch_wakeup.clear()
                    continue

                requested = set(due_ids)
                response = await self._post_omnibot_site_json(
                    "/api/omnibot/qq/outbox/watch",
                    {"ids": due_ids},
                )
                if response is None:
                    retry_at = time.time() + self.guess_result_watch_retry_seconds
                    for target_id in due_ids:
                        if target_id in self._guess_watches:
                            if float(
                                self._guess_watches[target_id].get("expires_at", 0)
                            ) <= time.time():
                                # 截止前最后一次主站请求也失败，停止本地监听。
                                self._guess_watches.pop(target_id, None)
                            else:
                                self._guess_watches[target_id]["next_check_at"] = retry_at
                    self._prune_expired_guess_watches(time.time())
                    continue

                returned: set[int] = set()
                deliveries = response.get("deliveries")
                if not isinstance(deliveries, list):
                    deliveries = []
                for delivery in deliveries:
                    if not isinstance(delivery, dict):
                        continue
                    target_id = _as_int(delivery.get("target_id"), 0)
                    if target_id <= 0:
                        continue
                    returned.add(target_id)
                    sent, error_code, error = await self._send_guess_result_delivery(
                        delivery
                    )
                    if sent:
                        if await self._ack_guess_result(target_id, "sent"):
                            self._guess_watches.pop(target_id, None)
                        elif target_id in self._guess_watches:
                            watch = self._guess_watches[target_id]
                            watch["awaiting_ack"] = True
                            watch["next_check_at"] = (
                                time.time() + self.guess_result_watch_retry_seconds
                            )
                    else:
                        await self._ack_guess_result(
                            target_id,
                            "failed",
                            error_code,
                            error,
                        )
                        if target_id in self._guess_watches:
                            self._guess_watches[target_id]["next_check_at"] = (
                                time.time() + self.guess_result_watch_retry_seconds
                            )

                watches = response.get("watches")
                if not isinstance(watches, list):
                    watches = []
                for watch_data in watches:
                    if not isinstance(watch_data, dict):
                        continue
                    target_id = _as_int(watch_data.get("target_id"), 0)
                    if target_id <= 0:
                        continue
                    returned.add(target_id)
                    watch = self._guess_watches.get(target_id)
                    if watch is None:
                        continue
                    next_check_at = self._parse_iso_timestamp(
                        watch_data.get("next_check_at")
                    )
                    expires_at = self._parse_iso_timestamp(watch_data.get("expires_at"))
                    if next_check_at is not None:
                        watch["next_check_at"] = next_check_at
                    if expires_at is not None:
                        watch["expires_at"] = expires_at

                expired_ids = response.get("expired_ids")
                if not isinstance(expired_ids, list):
                    expired_ids = []
                for target_id in expired_ids:
                    self._guess_watches.pop(_as_int(target_id, 0), None)

                for target_id in requested - returned:
                    self._guess_watches.pop(target_id, None)

                self._prune_expired_guess_watches(time.time())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.error("猜大小开奖监听异常：%s", exc)
        finally:
            if not self._guess_watches:
                self._guess_watch_task = None
            elif self.enabled and self.guess_result_watch_enabled:
                # 异常退出时保留活动目标；延迟重启，避免格式错误等异常形成忙循环。
                self._guess_watch_task = asyncio.create_task(
                    self._restart_guess_result_watch_loop(),
                    name="omnibot-guess-result-watch-restart",
                )

    async def _restart_guess_result_watch_loop(self) -> None:
        try:
            await asyncio.sleep(self.guess_result_watch_retry_seconds)
        except asyncio.CancelledError:
            return
        if (
            self._guess_watches
            and self.enabled
            and self.guess_result_watch_enabled
        ):
            self._guess_watch_wakeup.set()
            self._guess_watch_task = asyncio.create_task(
                self._run_guess_result_watch_loop(),
                name="omnibot-guess-result-watch",
            )

    async def _send_guess_result_delivery(
        self, delivery: dict[str, Any]
    ) -> tuple[bool, str, str]:
        target_id = _as_int(delivery.get("target_id"), 0)
        chat_type = _as_str(delivery.get("chat_type")).strip()
        chat_id = _as_str(delivery.get("chat_id")).strip()
        chat_openid = _as_str(delivery.get("chat_openid")).strip()
        raw_messages = delivery.get("messages")
        if not isinstance(raw_messages, list):
            raw_messages = []
        messages = [
            _as_str(message)
            for message in raw_messages
            if _as_str(message).strip()
        ]
        if target_id <= 0 or not chat_id or not messages:
            return False, "invalid_delivery", "通知消息为空"

        session = None
        if chat_type == "group":
            openid = chat_openid or self._from_omnibot_group_id(chat_id)
            session = self._session_by_group.get(chat_id)
            if session is None and openid:
                session = self._session_by_group.get(openid)
            if session is None:
                return False, "unknown_group", f"暂无群会话：{chat_id}"
        elif chat_type == "private":
            session = self._session_by_user.get(chat_id)
            if session is None:
                return False, "unknown_user", f"暂无私聊会话：{chat_id}"
        else:
            return False, "invalid_chat_type", f"未知会话类型：{chat_type}"

        for message in messages:
            try:
                chain = MessageChain(
                    chain=[
                        Comp.Plain(
                            text=self._fit_markdown_length(message)
                        )
                    ],
                    use_markdown_=True,
                )
                await self.context.send_message(session, chain)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "猜大小开奖 Markdown 发送失败，改用纯文本：%s", exc
                )
                try:
                    plain_chain = MessageChain(
                        chain=[Comp.Plain(text=message)],
                        use_markdown_=False,
                    )
                    await self.context.send_message(session, plain_chain)
                except Exception as fallback_exc:  # noqa: BLE001
                    return (
                        False,
                        "send_failed",
                        f"{type(fallback_exc).__name__}: {fallback_exc}",
                    )

        return True, "", ""

    async def _ack_guess_result(
        self,
        target_id: int,
        status: str,
        error_code: str = "",
        error: str = "",
    ) -> bool:
        response = await self._post_omnibot_site_json(
            "/api/omnibot/qq/outbox/ack",
            {
                "deliveries": [
                    {
                        "id": target_id,
                        "status": status,
                        "error_code": error_code,
                        "error": error[:500],
                    }
                ]
            },
        )
        return response is not None

    async def _post_omnibot_site_json(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.site_url or not self.secret:
            return None
        try:
            async with self._ensure_client().post(
                f"{self.site_url}{path}",
                headers={"X-API-Key": self.secret},
                json=payload,
                timeout=ClientTimeout(total=self.timeout),
            ) as response:
                text = await response.text()
                if response.status >= 300:
                    self.logger.error(
                        "OmniBot 开奖接口失败：HTTP %s %s",
                        response.status,
                        text[:300],
                    )
                    return None
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    self.logger.error("OmniBot 开奖接口返回了无效 JSON")
                    return None
                return data if isinstance(data, dict) else None
        except asyncio.TimeoutError:
            self.logger.error("OmniBot 开奖接口超时（%ss）", self.timeout)
        except ClientError as exc:
            self.logger.error("OmniBot 开奖接口网络错误：%s", exc)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("OmniBot 开奖接口异常：%s", exc)
        return None

    def _prune_expired_guess_watches(self, now: float) -> None:
        expired = [
            target_id
            for target_id, watch in self._guess_watches.items()
            if float(watch.get("expires_at", 0)) <= now
        ]
        for target_id in expired:
            self._guess_watches.pop(target_id, None)
        if expired:
            self.logger.info("猜大小开奖监听已到期并停止：%s", len(expired))

    @staticmethod
    def _parse_iso_timestamp(value: Any) -> float | None:
        text = _as_str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    @staticmethod
    def _format_timestamp(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().isoformat(
            timespec="seconds"
        )

    def _ensure_client(self) -> aiohttp.ClientSession:
        if self._client is None or self._client.closed:
            self._client = aiohttp.ClientSession(
                timeout=ClientTimeout(total=self.timeout)
            )
        return self._client

    async def _close_client(self) -> None:
        if self._client is not None and not self._client.closed:
            await self._client.close()
        self._client = None

    async def _send_reply(
        self,
        event: AstrMessageEvent,
        reply_text: str,
        is_group: bool,
        user_id: str,
    ) -> None:
        prefix = self.reply_prefix.strip()
        text = f"{prefix}{reply_text}" if prefix else reply_text
        chain: list = []
        is_onebot = self._is_onebot_event(event)
        if is_group and user_id and is_onebot:
            chain.append(Comp.At(qq=_to_int_if_possible(user_id)))

        # 只有支持 markdown 的平台才重排；其余平台保持 OmniBot 原文，
        # 避免把 `##`、`|` 这类语法原样发给不渲染 markdown 的客户端。
        platform_name = event.get_platform_name()
        parser = self._message_parser_for_event(event)
        markdown_platforms = self.reply_markdown_platforms
        use_markdown = (
            self.reply_markdown_enabled
            and (
                platform_name in markdown_platforms
                or (
                    parser == MESSAGE_PARSER_QQ_OFFICIAL
                    and MESSAGE_PARSER_QQ_OFFICIAL in markdown_platforms
                )
            )
        )
        markdown_text = (
            omnibot_text_to_markdown(
                text,
                self.reply_markdown_max_rows,
                self.reply_markdown_lottery_max_rows,
            )
            if use_markdown
            else text
        )
        if markdown_text:
            chain.append(Comp.Plain(text=self._fit_markdown_length(markdown_text)))
        if not chain:
            return
        try:
            # 直接发送 markdown 文本，不再转图。
            # AstrBot 在平台明确拒绝 markdown 时会自动回退为纯文本 content。
            await event.send(
                MessageChain(
                    chain=chain,
                    use_markdown_=use_markdown,
                )
            )
        except Exception as exc:  # noqa: BLE001
            if not use_markdown:
                self.logger.error("代发 OmniBot 回复失败：%s", exc)
                return
            # markdown 被拒（例如平台未开通或语法越界）时，用原始纯文本兜底，
            # 至少保证用户能看到回复内容。
            self.logger.warning(
                "markdown 发送被拒绝，改用纯文本重发：%s", exc
            )
            fallback: list = []
            if is_group and user_id and is_onebot:
                fallback.append(Comp.At(qq=_to_int_if_possible(user_id)))
            fallback.append(Comp.Plain(text=text))
            try:
                await event.send(MessageChain(chain=fallback, use_markdown_=False))
            except Exception as fallback_exc:  # noqa: BLE001
                self.logger.error("代发 OmniBot 回复失败：%s", fallback_exc)

    @staticmethod
    def _fit_markdown_length(text: str, limit: int = MARKDOWN_MAX_CHARS) -> str:
        """按字符数截断，避免超出平台 markdown 长度上限后整条发送失败。"""
        if len(text) <= limit:
            return text
        return f"{text[:limit]}\n\n> 内容过长，已截断显示。"

    # ------------------------------------------------------------------
    # 主动发送所需的会话映射
    # ------------------------------------------------------------------
    def _remember_session(
        self, group_id: str, user_id: str, session: Any, is_group: bool
    ) -> None:
        self._session_seen_at[str(session)] = time.time()
        if is_group and group_id:
            self._session_by_group[group_id] = session
            # QQ 官方群成员消息里可能没有可用的私聊会话，不能把群会话
            # 当作该成员的私聊会话，否则会把私信误发到群里。
            if self._is_private_session(session):
                self._session_by_user[user_id] = session
        elif user_id:
            self._session_by_user[user_id] = session
        self._prune_session_cache()

    @staticmethod
    def _is_private_session(session: Any) -> bool:
        message_type = getattr(session, "message_type", None)
        return message_type == MessageType.FRIEND_MESSAGE

    def _prune_session_cache(self) -> None:
        overflow = len(self._session_seen_at) - MAX_SESSION_CACHE
        if overflow <= 0:
            return
        oldest = sorted(self._session_seen_at.items(), key=lambda kv: kv[1])[:overflow]
        dropped = {session_key for session_key, _ in oldest}
        for session_key in dropped:
            self._session_seen_at.pop(session_key, None)
        self._session_by_group = {
            key: value
            for key, value in self._session_by_group.items()
            if str(value) not in dropped
        }
        self._session_by_user = {
            key: value
            for key, value in self._session_by_user.items()
            if str(value) not in dropped
        }
