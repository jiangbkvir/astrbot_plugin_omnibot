from __future__ import annotations

import asyncio
import importlib.util
import time
import unittest
from pathlib import Path


PLUGIN_MAIN = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("omnibot_plugin_main_test", PLUGIN_MAIN)
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


class GuessResultWatchTest(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = object.__new__(OMNIBOT.OmniBotGateway)
        plugin.config = {
            "enabled": True,
            "guess_result_watch_enabled": True,
            "guess_result_watch_retry_seconds": 10,
            "omnibot_site_url": "https://example.test",
            "qq_bot_secret": "test-secret",
        }
        plugin.logger = FakeLogger()
        plugin._guess_watches = {}
        plugin._guess_watch_task = None
        plugin._guess_watch_wakeup = asyncio.Event()
        plugin._session_by_group = {}
        plugin._session_by_user = {}
        return plugin

    async def test_no_watch_metadata_does_not_start_requests(self) -> None:
        plugin = self.make_plugin()
        calls = 0

        async def fake_post(path, payload):
            nonlocal calls
            calls += 1
            return {"deliveries": [], "watches": [], "expired_ids": []}

        plugin._post_omnibot_site_json = fake_post
        plugin._register_guess_result_watch({})

        self.assertEqual({}, plugin._guess_watches)
        self.assertIsNone(plugin._guess_watch_task)
        self.assertEqual(0, calls)

    async def test_delivery_is_sent_acknowledged_and_then_stops(self) -> None:
        plugin = self.make_plugin()
        send_calls = 0
        ack_calls = 0

        async def fake_post(path, payload):
            return {
                "deliveries": [
                    {
                        "target_id": 7,
                        "chat_type": "group",
                        "chat_id": "1130256473",
                        "messages": ["## 开奖结果"],
                    }
                ],
                "watches": [],
                "expired_ids": [],
            }

        async def fake_send(delivery):
            nonlocal send_calls
            send_calls += 1
            return True, "", ""

        async def fake_ack(target_id, status, error_code="", error=""):
            nonlocal ack_calls
            ack_calls += 1
            return True

        plugin._post_omnibot_site_json = fake_post
        plugin._send_guess_result_delivery = fake_send
        plugin._ack_guess_result = fake_ack
        plugin._register_guess_result_watch(
            {
                "guess_result_watch": {
                    "target_id": 7,
                    "chat_type": "group",
                    "chat_id": "1130256473",
                    "next_check_at": "2026-10-30T00:00:00+00:00",
                    "expires_at": "2099-10-30T00:00:00+00:00",
                }
            }
        )
        self.assertIsNotNone(plugin._guess_watch_task)
        await asyncio.wait_for(plugin._guess_watch_task, timeout=2)

        self.assertEqual(1, send_calls)
        self.assertEqual(1, ack_calls)
        self.assertEqual({}, plugin._guess_watches)

    async def test_first_check_waits_for_server_next_check_time(self) -> None:
        plugin = self.make_plugin()
        calls = 0

        async def fake_post(path, payload):
            nonlocal calls
            calls += 1
            return {"deliveries": [], "watches": [], "expired_ids": []}

        plugin._post_omnibot_site_json = fake_post
        next_check_at = time.time() + 0.3
        expires_at = time.time() + 3
        plugin._register_guess_result_watch(
            {
                "guess_result_watch": {
                    "target_id": 8,
                    "chat_type": "group",
                    "chat_id": "1130256473",
                    "next_check_at": OMNIBOT.datetime.fromtimestamp(
                        next_check_at, tz=OMNIBOT.timezone.utc
                    ).isoformat(),
                    "expires_at": OMNIBOT.datetime.fromtimestamp(
                        expires_at, tz=OMNIBOT.timezone.utc
                    ).isoformat(),
                }
            }
        )

        await asyncio.sleep(0.1)
        self.assertEqual(0, calls)
        await asyncio.wait_for(plugin._guess_watch_task, timeout=2)
        self.assertEqual(1, calls)
        self.assertEqual({}, plugin._guess_watches)

    async def test_expiry_triggers_one_final_watch_request(self) -> None:
        plugin = self.make_plugin()
        requested_ids: list[int] = []

        async def fake_post(path, payload):
            requested_ids.extend(payload["ids"])
            return {
                "deliveries": [],
                "watches": [],
                "expired_ids": [9],
            }

        plugin._post_omnibot_site_json = fake_post
        expires_at = time.time() + 0.2
        plugin._register_guess_result_watch(
            {
                "guess_result_watch": {
                    "target_id": 9,
                    "chat_type": "group",
                    "chat_id": "1130256473",
                    "next_check_at": OMNIBOT.datetime.fromtimestamp(
                        expires_at, tz=OMNIBOT.timezone.utc
                    ).isoformat(),
                    "expires_at": OMNIBOT.datetime.fromtimestamp(
                        expires_at, tz=OMNIBOT.timezone.utc
                    ).isoformat(),
                }
            }
        )

        await asyncio.wait_for(plugin._guess_watch_task, timeout=2)
        self.assertEqual([9], requested_ids)
        self.assertEqual({}, plugin._guess_watches)

    async def test_unknown_group_fails_without_sending(self) -> None:
        plugin = self.make_plugin()

        sent, error_code, error = await plugin._send_guess_result_delivery(
            {
                "target_id": 10,
                "chat_type": "group",
                "chat_id": "1130256473",
                "messages": ["## 开奖结果"],
            }
        )

        self.assertFalse(sent)
        self.assertEqual("unknown_group", error_code)
        self.assertIn("1130256473", error)


if __name__ == "__main__":
    unittest.main()
