from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Any


PLUGIN_MAIN = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("omnibot_plugin_official_reply_test", PLUGIN_MAIN)
assert SPEC is not None and SPEC.loader is not None
OMNIBOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OMNIBOT)


class FakeLogger:
    def warning(self, message: str, *args) -> None:
        pass

    def error(self, message: str, *args) -> None:
        pass


class FakeMessageObject:
    def __init__(
        self,
        *,
        message_id: str = "ROBOT1.0_INBOUND",
        group_id: str = "GROUP_OPENID",
    ) -> None:
        self.message_id = message_id
        self.group_id = group_id
        self.raw_message = None
        self.sender = None


class FakeEvent:
    def __init__(
        self,
        platform_name: str,
        *,
        sender_name: str = "测试用户",
        message_id: str = "ROBOT1.0_INBOUND",
    ) -> None:
        self.platform_name = platform_name
        self.sender_name = sender_name
        self.message_obj = FakeMessageObject(message_id=message_id)

    def get_platform_name(self) -> str:
        return self.platform_name

    def get_sender_name(self) -> str:
        return self.sender_name


class QqOfficialReplyTest(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, **config):
        plugin = object.__new__(OMNIBOT.OmniBotGateway)
        plugin.config = {
            "message_parser": "auto",
            "reply_prefix": "",
            "reply_markdown_enabled": False,
            "reply_markdown_platforms": ["qq_official"],
            **config,
        }
        plugin.logger = FakeLogger()
        plugin.sent_chains: list[Any] = []

        async def capture_chain(event, chain):
            plugin.sent_chains.append(chain)
            return True, "OUTBOUND"

        plugin._send_event_chain = capture_chain
        return plugin

    async def test_official_group_reply_quotes_message_and_prefixes_sender(self):
        plugin = self.make_plugin()
        event = FakeEvent("qq_official")

        await plugin._send_reply(event, "绑定失败", is_group=True, user_id="USER_OPENID")

        chain = plugin.sent_chains[0].chain
        self.assertIsInstance(chain[0], OMNIBOT.Comp.Reply)
        self.assertEqual("ROBOT1.0_INBOUND", chain[0].id)
        self.assertIsInstance(chain[1], OMNIBOT.Comp.Plain)
        self.assertEqual("@测试用户\n绑定失败", chain[1].text)

    async def test_official_private_reply_quotes_without_group_sender_prefix(self):
        plugin = self.make_plugin()
        event = FakeEvent("qq_official_webhook")

        await plugin._send_reply(event, "绑定失败", is_group=False, user_id="USER_OPENID")

        chain = plugin.sent_chains[0].chain
        self.assertIsInstance(chain[0], OMNIBOT.Comp.Reply)
        self.assertEqual("绑定失败", chain[1].text)

    async def test_onebot_group_reply_keeps_real_at_without_name_prefix(self):
        plugin = self.make_plugin(message_parser="onebot")
        event = FakeEvent("aiocqhttp")

        await plugin._send_reply(event, "查询结果", is_group=True, user_id="123456")

        chain = plugin.sent_chains[0].chain
        self.assertIsInstance(chain[0], OMNIBOT.Comp.At)
        self.assertEqual(123456, chain[0].qq)
        self.assertEqual("查询结果", chain[1].text)

    async def test_official_markdown_fallback_keeps_quote_and_sender_prefix(self):
        plugin = self.make_plugin(reply_markdown_enabled=True)
        event = FakeEvent("qq_official")
        calls = 0

        async def fail_markdown_then_capture(event_arg, chain):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("markdown unsupported")
            plugin.sent_chains.append(chain)
            return True, "OUTBOUND"

        plugin._send_event_chain = fail_markdown_then_capture

        await plugin._send_reply(event, "绑定失败", is_group=True, user_id="USER_OPENID")

        fallback = plugin.sent_chains[0].chain
        self.assertIsInstance(fallback[0], OMNIBOT.Comp.Reply)
        self.assertEqual("@测试用户\n绑定失败", fallback[1].text)
        self.assertFalse(plugin.sent_chains[0].use_markdown_)


if __name__ == "__main__":
    unittest.main()
