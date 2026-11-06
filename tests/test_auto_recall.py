from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path
from typing import Any


PLUGIN_MAIN = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("omnibot_plugin_recall_test", PLUGIN_MAIN)
assert SPEC is not None and SPEC.loader is not None
OMNIBOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OMNIBOT)


class FakeLogger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def info(self, message: str, *args) -> None:
        self.messages.append(("info", message % args if args else message))

    def warning(self, message: str, *args) -> None:
        self.messages.append(("warning", message % args if args else message))

    def error(self, message: str, *args) -> None:
        self.messages.append(("error", message % args if args else message))


class FakeOneBotBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.actions: list[tuple[str, dict[str, Any]]] = []

    async def send_group_msg(self, **kwargs):
        self.sent.append(("group", kwargs))
        return {"status": "ok", "data": {"message_id": 1001}}

    async def send_private_msg(self, **kwargs):
        self.sent.append(("private", kwargs))
        return {"status": "ok", "data": {"message_id": 1002}}

    async def call_action(self, action: str, **kwargs):
        self.actions.append((action, kwargs))
        return {}


class FakeMeta:
    def __init__(self, name: str, platform_id: str = "bot-1") -> None:
        self.name = name
        self.id = platform_id


class FakePlatform:
    def __init__(self, name: str, bot: Any, platform_id: str = "bot-1") -> None:
        self._name = name
        self.bot = bot
        self.client = bot
        self._platform_id = platform_id
        self._session_scene: dict[str, str] = {}

    def meta(self):
        return FakeMeta(self._name, self._platform_id)


class FakeSession:
    def __init__(self, session_id: str, message_type: Any) -> None:
        self.session_id = session_id
        self.message_type = message_type
        self.platform_id = "bot-1"


class FakePlatformManager:
    def __init__(self, platforms: list[Any]) -> None:
        self.platform_insts = platforms


class FakeContext:
    def __init__(self, platforms: list[Any]) -> None:
        self.platform_manager = FakePlatformManager(platforms)
        self.session_sends = 0

    async def send_message(self, session, chain) -> None:
        self.session_sends += 1


class FakeHttp:
    def __init__(self) -> None:
        self.requests: list[tuple[Any, dict[str, Any]]] = []

    async def request(self, route, **kwargs):
        self.requests.append((route, kwargs))
        return {}


class FakeOfficialApi:
    def __init__(self) -> None:
        self._http = FakeHttp()
        self.recalled: list[tuple[str, str]] = []

    async def recall_message(self, channel_id: str, message_id: str, **kwargs):
        self.recalled.append((channel_id, message_id))
        return None


class FakeOfficialBot:
    def __init__(self) -> None:
        self.api = FakeOfficialApi()


class AutoRecallTest(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, **config):
        plugin = object.__new__(OMNIBOT.OmniBotGateway)
        plugin.config = {
            "auto_recall_enabled": True,
            "auto_recall_delay_seconds": 15,
            **config,
        }
        plugin.logger = FakeLogger()
        plugin._recall_tasks = set()
        plugin._recall_warnings = set()
        return plugin

    async def test_onebot_group_and_private_sends_return_real_message_id(self):
        plugin = self.make_plugin()
        bot = FakeOneBotBot()
        chain = OMNIBOT.MessageChain(
            [
                OMNIBOT.Comp.At(qq=123456),
                OMNIBOT.Comp.Plain(text=" 查询结果"),
            ]
        )

        group_id = await plugin._send_onebot_chain(
            bot=bot,
            is_group=True,
            session_id="1130256473",
            chain=chain,
            self_id=999,
        )
        private_id = await plugin._send_onebot_chain(
            bot=bot,
            is_group=False,
            session_id="123456",
            chain=chain,
        )

        self.assertEqual("1001", group_id)
        self.assertEqual("1002", private_id)
        self.assertEqual(1130256473, bot.sent[0][1]["group_id"])
        self.assertEqual(999, bot.sent[0][1]["self_id"])
        self.assertEqual("at", bot.sent[0][1]["message"][0]["type"])
        self.assertEqual(123456, bot.sent[1][1]["user_id"])

    async def test_onebot_delete_msg_uses_sent_message_id(self):
        plugin = self.make_plugin()
        bot = FakeOneBotBot()
        target = OMNIBOT.RecallTarget(
            "aiocqhttp",
            "1001",
            "1130256473",
            "group",
            bot,
        )

        self.assertTrue(await plugin._recall_message(target))
        self.assertEqual(
            [("delete_msg", {"message_id": 1001})],
            bot.actions,
        )

    async def test_qq_official_group_and_c2c_recall_routes(self):
        plugin = self.make_plugin()
        bot = FakeOfficialBot()
        group = OMNIBOT.RecallTarget(
            "qq_official",
            "ROBOT1.0_GROUP",
            "GROUP_OPENID",
            "group",
            bot,
        )
        c2c = OMNIBOT.RecallTarget(
            "qq_official_webhook",
            "ROBOT1.0_C2C",
            "USER_OPENID",
            "c2c",
            bot,
        )

        self.assertTrue(await plugin._recall_message(group))
        self.assertTrue(await plugin._recall_message(c2c))

        first_route, first_kwargs = bot.api._http.requests[0]
        second_route, second_kwargs = bot.api._http.requests[1]
        self.assertEqual("DELETE", first_route.method)
        self.assertEqual(
            "/v2/groups/{group_openid}/messages/{message_id}",
            first_route.path,
        )
        self.assertEqual("GROUP_OPENID", first_route.parameters["group_openid"])
        self.assertEqual({}, first_kwargs)
        self.assertEqual("DELETE", second_route.method)
        self.assertEqual(
            "/v2/users/{user_openid}/messages/{message_id}",
            second_route.path,
        )
        self.assertEqual("USER_OPENID", second_route.parameters["user_openid"])
        self.assertEqual({}, second_kwargs)

    async def test_onebot_session_send_bypasses_context_and_schedules_recall(self):
        plugin = self.make_plugin(auto_recall_delay_seconds=30)
        bot = FakeOneBotBot()
        platform = FakePlatform("aiocqhttp", bot)
        plugin.context = FakeContext([platform])
        session = FakeSession(
            "1130256473",
            OMNIBOT.MessageType.GROUP_MESSAGE,
        )
        chain = OMNIBOT.MessageChain([OMNIBOT.Comp.Plain(text="主动通知")])

        sent, message_id = await plugin._send_session_chain(session, chain)

        self.assertTrue(sent)
        self.assertEqual("1001", message_id)
        self.assertEqual(0, plugin.context.session_sends)
        self.assertEqual(1, len(plugin._recall_tasks))
        await plugin._stop_auto_recall_tasks()
        self.assertEqual(set(), plugin._recall_tasks)

    async def test_qq_official_session_uses_adapter_message_id_cache(self):
        plugin = self.make_plugin(auto_recall_delay_seconds=30)
        bot = FakeOfficialBot()
        platform = FakePlatform("qq_official", bot)
        platform._session_last_message_id = {"GROUP_OPENID": "INBOUND-ID"}
        platform._session_scene = {"GROUP_OPENID": "group"}
        plugin.context = FakeContext([platform])
        session = FakeSession(
            "GROUP_OPENID",
            OMNIBOT.MessageType.GROUP_MESSAGE,
        )
        chain = OMNIBOT.MessageChain([OMNIBOT.Comp.Plain(text="主动通知")])

        async def fake_context_send(session_arg, chain_arg) -> None:
            plugin.context.session_sends += 1
            platform._session_last_message_id[session_arg.session_id] = "OUTBOUND-ID"

        plugin.context.send_message = fake_context_send
        sent, message_id = await plugin._send_session_chain(session, chain)

        self.assertTrue(sent)
        self.assertEqual("OUTBOUND-ID", message_id)
        self.assertEqual(1, plugin.context.session_sends)
        self.assertEqual(1, len(plugin._recall_tasks))
        await plugin._stop_auto_recall_tasks()

    async def test_disabled_recall_does_not_create_task(self):
        plugin = self.make_plugin(auto_recall_enabled=False)
        target = OMNIBOT.RecallTarget(
            "aiocqhttp",
            "1001",
            "1130256473",
            "group",
            FakeOneBotBot(),
        )

        plugin._schedule_message_recall(target)
        self.assertEqual(set(), plugin._recall_tasks)

    async def test_delay_is_clamped_to_safe_range(self):
        self.assertEqual(
            5,
            self.make_plugin(auto_recall_delay_seconds=-1).auto_recall_delay_seconds,
        )
        self.assertEqual(
            120,
            self.make_plugin(auto_recall_delay_seconds=999).auto_recall_delay_seconds,
        )
        self.assertEqual(
            15,
            self.make_plugin().auto_recall_delay_seconds,
        )


if __name__ == "__main__":
    unittest.main()
