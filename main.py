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

QQ 官方平台（``qq_official``）只有 openid。网关只把平台真实消息解析为 CQ 码，
不会把用户手打的 CQ 码当成真实消息，也不实现任何业务命令。
"""

from __future__ import annotations

import asyncio
import hmac
import re
import time
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from aiohttp import ClientError, ClientTimeout, web
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star

MAX_SESSION_CACHE = 2000
DEFAULT_GATEWAY_PORT = 8080
MAX_BODY_BYTES = 1024 * 1024
MIN_SECRET_BYTES = 16
GATEWAY_HANDLER_PRIORITY = 10000

_CQ_PREFIX_RE = re.compile(r"\[\s*cq\s*:", re.IGNORECASE)


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
    def gateway_base_url(self) -> str:
        host = self.gateway_host
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        return f"http://{host}:{self.gateway_port}"

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

    async def terminate(self) -> None:
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
    # QQ -> 网关 -> OmniBot
    # ------------------------------------------------------------------
    # 网关必须先于 AstrBot 的聊天开关、AI 和其他群管插件拿到原始事件，
    # 否则 stop_event() 会让 OmniBot 指令根本没有机会转发。
    @filter.event_message_type(
        filter.EventMessageType.ALL,
        priority=GATEWAY_HANDLER_PRIORITY,
    )
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.QQOFFICIAL,
        priority=GATEWAY_HANDLER_PRIORITY,
    )
    async def on_qq_message(self, event: AstrMessageEvent):
        """转发 QQ 消息，不解析、不执行任何业务命令。"""
        if not self.enabled or not self.site_url:
            return
        if not self.secret:
            self.logger.error("qq_bot_secret 为空，拒绝转发 QQ 消息。")
            event.stop_event()
            return
        if event.get_message_type() not in (
            MessageType.GROUP_MESSAGE,
            MessageType.FRIEND_MESSAGE,
        ):
            return

        msg_obj = event.message_obj
        group_id = str(getattr(msg_obj, "group_id", "") or "")
        is_group = bool(group_id)
        user_id = event.get_sender_id()

        allowed = self.allowed_groups
        if is_group and allowed and group_id not in allowed:
            return
        if not is_group and not self.forward_private:
            return
        if not user_id:
            return

        self._remember_session(group_id, user_id, event.session, is_group)
        raw_message = self._build_raw_message(event)
        if not raw_message:
            self.logger.debug("跳过无消息内容的事件：group=%s user=%s", group_id, user_id)
            return

        self.logger.info(
            "OmniBot 网关命中：type=%s group=%s user=%s",
            "group" if is_group else "private",
            group_id or "-",
            user_id,
        )
        payload = self._build_onebot_event(event, msg_obj, is_group, raw_message)
        reply_text = await self._post_to_omnibot(payload)
        if reply_text is None:
            return
        self.logger.info(
            "OmniBot webhook 成功：type=%s group=%s user=%s has_reply=%s",
            "group" if is_group else "private",
            group_id or "-",
            user_id,
            bool(reply_text),
        )

        if self.stop_event_after_forward:
            event.stop_event()
        if reply_text:
            await self._send_reply(event, reply_text, is_group, user_id)

    def _build_raw_message(self, event: AstrMessageEvent) -> str:
        """从平台真实消息构建 raw_message，绝不信任用户手写的 CQ 码。"""
        raw = getattr(event.message_obj, "raw_message", None)

        # OneBot：优先使用协议端的消息段/原始 CQ 字符串。
        if event.get_platform_name() == "aiocqhttp":
            if isinstance(raw, dict):
                message = raw.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
                if isinstance(message, list):
                    rebuilt = self._segments_to_cq(message)
                    if rebuilt:
                        return rebuilt
                raw_str = raw.get("raw_message")
                if isinstance(raw_str, str) and raw_str.strip():
                    return raw_str.strip()
            rebuilt = self._chain_to_cq(getattr(event.message_obj, "message", None))
            return rebuilt or (event.message_str or "").strip()

        # QQ 官方：消息链中的 At 才是平台真实 mention。仅中和 Plain 文本里的 CQ 码。
        rebuilt = self._chain_to_cq(
            getattr(event.message_obj, "message", None),
            neutralize_plain=event.get_platform_name() == "qq_official",
        )
        if rebuilt:
            return rebuilt
        return _neutralize_cq_codes((event.message_str or "").strip())

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
    def _chain_to_cq(cls, chain: list | None, *, neutralize_plain: bool = False) -> str:
        if not chain:
            return ""
        parts: list[str] = []
        for comp in chain:
            if isinstance(comp, Comp.Plain):
                text = _as_str(getattr(comp, "text", ""))
                parts.append(_neutralize_cq_codes(text) if neutralize_plain else text)
            elif isinstance(comp, Comp.At):
                qq = _as_str(getattr(comp, "qq", ""))
                if qq:
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
        user_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        message_id = _as_str(getattr(msg_obj, "message_id", ""))

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
        if is_group:
            payload["group_id"] = _to_int_if_possible(group_id)
        payload["_secret"] = self.secret
        return payload

    async def _post_to_omnibot(self, payload: dict) -> str | None:
        timeout = ClientTimeout(total=self.timeout)
        try:
            async with self._ensure_client().post(
                self.webhook_url, json=payload, timeout=timeout
            ) as resp:
                if resp.status >= 300:
                    self.logger.error("OmniBot webhook 返回 HTTP %s", resp.status)
                    return None
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return ""
        except asyncio.TimeoutError:
            self.logger.error("请求 OmniBot webhook 超时（%ss）", self.timeout)
            return None
        except ClientError as exc:
            self.logger.error("请求 OmniBot webhook 网络错误：%s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            self.logger.error("请求 OmniBot webhook 出现未知错误：%s", exc)
            return None

        if not isinstance(data, dict) or not self.reply_from_webhook_json:
            return ""
        return _as_str(data.get("reply")).strip()

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
        if is_group and user_id and event.get_platform_name() == "aiocqhttp":
            chain.append(Comp.At(qq=_to_int_if_possible(user_id)))
        if text:
            chain.append(Comp.Plain(text=text))
        if not chain:
            return
        try:
            await event.send(MessageChain(chain=chain))
        except Exception as exc:  # noqa: BLE001
            self.logger.error("代发 OmniBot 回复失败：%s", exc)

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
