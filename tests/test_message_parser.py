from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Any


PLUGIN_MAIN = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("omnibot_plugin_parser_test", PLUGIN_MAIN)
assert SPEC is not None and SPEC.loader is not None
OMNIBOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OMNIBOT)


class FakeLogger:
    def info(self, message: str, *args) -> None:
        pass

    def warning(self, message: str, *args) -> None:
        pass

    def error(self, message: str, *args) -> None:
        pass


class FakeMessageObject:
    def __init__(
        self,
        *,
        group_id: str = "",
        raw_message: Any = None,
        message: list[Any] | None = None,
        message_id: str = "10001",
    ) -> None:
        self.group_id = group_id
        self.raw_message = raw_message
        self.message = message or []
        self.message_id = message_id


class FakeEvent:
    def __init__(
        self,
        platform_name: str,
        message_obj: FakeMessageObject,
        *,
        sender_id: str = "123456789",
        sender_name: str = "测试用户",
        self_id: str = "987654321",
        message_str: str = "",
    ) -> None:
        self.platform_name = platform_name
        self.message_obj = message_obj
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.self_id = self_id
        self.message_str = message_str

    def get_platform_name(self) -> str:
        return self.platform_name

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_sender_name(self) -> str:
        return self.sender_name

    def get_self_id(self) -> str:
        return self.self_id


class RawOfficialMessage:
    def __init__(self, mentions: list[dict[str, Any]] | None = None) -> None:
        self.mentions = mentions or []


class MessageParserTest(unittest.TestCase):
    def make_plugin(self, **config):
        plugin = object.__new__(OMNIBOT.OmniBotGateway)
        plugin.config = config
        plugin.logger = FakeLogger()
        plugin._qq_panel_command_names = set()
        return plugin

    def test_parser_aliases_map_to_supported_modes(self) -> None:
        aliases = {
            "auto": "auto",
            "onebot": "onebot",
            "onebot_v11": "onebot",
            "aiocqhttp": "onebot",
            "NapCat": "onebot",
            "LLOneBot": "onebot",
            "lagrange": "onebot",
            "qq_official": "qq_official",
            "qq_official_webhook": "qq_official",
        }
        for configured, expected in aliases.items():
            with self.subTest(configured=configured):
                plugin = self.make_plugin(message_parser=configured)
                self.assertEqual(expected, plugin.message_parser)

    def test_auto_detects_onebot_and_qq_official_platforms(self) -> None:
        plugin = self.make_plugin(message_parser="auto")
        onebot_event = FakeEvent(
            "aiocqhttp",
            FakeMessageObject(group_id="1130256473"),
        )
        official_event = FakeEvent(
            "qq_official_webhook",
            FakeMessageObject(group_id="GROUP_OPENID"),
        )

        self.assertEqual("onebot", plugin._message_parser_for_event(onebot_event))
        self.assertEqual(
            "qq_official",
            plugin._message_parser_for_event(official_event),
        )

    def test_onebot_builds_numeric_group_and_user_without_openid_fields(self) -> None:
        plugin = self.make_plugin(
            message_parser="onebot",
            qq_bot_secret="test-secret",
            qq_group_id_map={},
        )
        event = FakeEvent(
            "aiocqhttp",
            FakeMessageObject(
                group_id="1130256473",
                raw_message={
                    "post_type": "message",
                    "message_type": "group",
                    "message": "[CQ:at,qq=555555] +100",
                },
            ),
        )

        raw_message = plugin._build_raw_message(event)
        payload = plugin._build_onebot_event(
            event,
            event.message_obj,
            True,
            raw_message,
        )

        self.assertEqual("[CQ:at,qq=555555] +100", raw_message)
        self.assertEqual(123456789, payload["user_id"])
        self.assertEqual(1130256473, payload["group_id"])
        self.assertNotIn("group_openid", payload)
        self.assertNotIn("sender_openid", payload)
        self.assertNotIn("member_openid", payload)

    def test_onebot_supports_segment_list_from_all_three_protocols(self) -> None:
        plugin = self.make_plugin(message_parser="aiocqhttp")
        event = FakeEvent(
            "aiocqhttp",
            FakeMessageObject(
                group_id="1130256473",
                raw_message={
                    "post_type": "message",
                    "message_type": "group",
                    "message": [
                        {"type": "at", "data": {"qq": "555555"}},
                        {"type": "text", "data": {"text": " +100"}},
                    ],
                },
            ),
        )

        self.assertEqual(
            "[CQ:at,qq=555555] +100",
            plugin._build_raw_message(event),
        )

    def test_onebot_removes_self_mention_before_command(self) -> None:
        plugin = self.make_plugin(message_parser="onebot")
        event = FakeEvent(
            "aiocqhttp",
            FakeMessageObject(
                group_id="1130256473",
                raw_message={
                    "post_type": "message",
                    "message_type": "group",
                    "message": "[CQ:at,qq=987654321] /guess 大 1000",
                },
            ),
        )

        self.assertEqual(
            "/guess 大 1000",
            plugin._build_raw_message(event),
        )

    def test_onebot_removes_self_mention_with_extra_cq_parameters(self) -> None:
        plugin = self.make_plugin(message_parser="onebot")
        event = FakeEvent(
            "aiocqhttp",
            FakeMessageObject(
                group_id="1130256473",
                raw_message={
                    "post_type": "message",
                    "message_type": "group",
                    "raw_message": (
                        "[CQ:at,qq=987654321,need_at=true] /help"
                    ),
                    "message": [],
                },
            ),
        )

        self.assertEqual("/help", plugin._build_raw_message(event))

    def test_onebot_uses_raw_event_self_id_when_message_object_omits_it(self) -> None:
        plugin = self.make_plugin(message_parser="onebot")
        event = FakeEvent(
            "aiocqhttp",
            FakeMessageObject(
                group_id="1130256473",
                raw_message={
                    "post_type": "message",
                    "message_type": "group",
                    "self_id": 987654321,
                    "message": [
                        {"type": "at", "data": {"qq": 987654321}},
                        {"type": "text", "data": {"text": " /help"}},
                    ],
                },
            ),
            self_id="",
        )

        self.assertEqual("/help", plugin._build_raw_message(event))

    def test_onebot_removes_self_mention_from_segment_but_keeps_member_mention(self) -> None:
        plugin = self.make_plugin(message_parser="onebot")
        event = FakeEvent(
            "aiocqhttp",
            FakeMessageObject(
                group_id="1130256473",
                raw_message={
                    "post_type": "message",
                    "message_type": "group",
                    "message": [
                        {"type": "at", "data": {"qq": "987654321"}},
                        {"type": "text", "data": {"text": " "}},
                        {"type": "at", "data": {"qq": "555555"}},
                        {"type": "text", "data": {"text": " +100"}},
                    ],
                },
            ),
        )

        self.assertEqual(
            "[CQ:at,qq=555555] +100",
            plugin._build_raw_message(event),
        )

    def test_qq_official_keeps_openid_and_real_mentions(self) -> None:
        plugin = self.make_plugin(
            message_parser="qq_official",
            qq_bot_secret="test-secret",
            qq_group_id_map={"GROUP_OPENID": "1130256473"},
        )
        raw = RawOfficialMessage(
            mentions=[
                {"id": "987654321", "is_you": True},
                {"id": "MEMBER_OPENID", "is_you": False},
            ]
        )
        event = FakeEvent(
            "qq_official",
            FakeMessageObject(
                group_id="GROUP_OPENID",
                raw_message=raw,
                message=[
                    OMNIBOT.Comp.At(qq="987654321"),
                    OMNIBOT.Comp.At(qq="MEMBER_OPENID"),
                    OMNIBOT.Comp.Plain(text=" +100"),
                ],
            ),
            sender_id="SENDER_OPENID",
        )

        raw_message = plugin._build_raw_message(event)
        payload = plugin._build_onebot_event(
            event,
            event.message_obj,
            True,
            raw_message,
        )

        self.assertEqual("[CQ:at,qq=MEMBER_OPENID] +100", raw_message)
        self.assertEqual(1130256473, payload["group_id"])
        self.assertEqual("GROUP_OPENID", payload["group_openid"])
        self.assertEqual("SENDER_OPENID", payload["member_openid"])
        self.assertEqual("SENDER_OPENID", payload["sender_openid"])


if __name__ == "__main__":
    unittest.main()
