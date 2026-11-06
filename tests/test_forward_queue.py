from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path
from typing import Any


PLUGIN_MAIN = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("omnibot_plugin_queue_test", PLUGIN_MAIN)
assert SPEC is not None and SPEC.loader is not None
OMNIBOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OMNIBOT)


class FakeLogger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def _record(self, level: str, message: str, *args: Any) -> None:
        self.messages.append((level, message))

    def info(self, message: str, *args: Any) -> None:
        self._record("info", message, *args)

    def warning(self, message: str, *args: Any) -> None:
        self._record("warning", message, *args)

    def error(self, message: str, *args: Any) -> None:
        self._record("error", message, *args)


class FakeEvent:
    def __init__(self, message_type: str = "group", user_id: str = "111") -> None:
        self.message_type = message_type
        self.user_id = user_id
        self.stopped = False

    def stop_event(self) -> None:
        self.stopped = True


class FakeMessageObject:
    def __init__(self, group_id: str = "20001") -> None:
        self.group_id = group_id
        self.raw_message = "hello"
        self.message_id = "90001"


def make_plugin(**config: Any) -> Any:
    defaults = {
        "enabled": True,
        "forward_to_omnibot": True,
        "omnibot_site_url": "http://127.0.0.1:8004",
        "qq_bot_secret": "a-very-long-secret-key-123",
        "message_parser": "onebot",
        "qq_group_id_map": {},
    }
    defaults.update(config)
    plugin = object.__new__(OMNIBOT.OmniBotGateway)
    plugin.config = defaults
    plugin.logger = FakeLogger()
    plugin._qq_panel_command_names = set()
    plugin._forward_queue_size = max(
        1, int(defaults.get("forward_queue_size", 200))
    )
    return plugin


class ForwardQueueTest(unittest.IsolatedAsyncioTestCase):
    def dummy_plugin(self) -> Any:
        plugin = make_plugin(forward_queue_size=3)
        plugin._forward_queue = asyncio.Queue(maxsize=3)
        return plugin

    async def test_enqueue_accepts_entry_and_marks_stopped(self) -> None:
        plugin = self.dummy_plugin()
        event = FakeEvent("group", "aaa")
        payload = {"group_id": 20001, "user_id": 111}
        self.assertTrue(plugin._enqueue_forward(event, payload))
        self.assertTrue(event.stopped)  # 需与 on_qq_message 里的 stop_event 保持一致
        self.assertEqual(1, plugin._forward_queue.qsize())

    async def test_enqueue_when_full_drops_oldest_and_keeps_newest(self) -> None:
        plugin = self.dummy_plugin()
        first = FakeEvent("group", "first")
        second = FakeEvent("group", "second")
        third = FakeEvent("group", "third")
        for ev in (first, second, third):
            plugin._enqueue_forward(ev, {"user_id": ev.user_id})
        self.assertEqual(3, plugin._forward_queue.qsize())

        newest = FakeEvent("group", "newest")
        ok = plugin._enqueue_forward(newest, {"user_id": newest.user_id})
        self.assertTrue(ok)
        self.assertEqual(3, plugin._forward_queue.qsize())

        remaining = [plugin._forward_queue.get_nowait() for _ in range(3)]
        plugin._forward_queue.task_done()
        remaining_user_ids = {ev.user_id for ev, _ in remaining}
        self.assertEqual({"second", "third", "newest"}, remaining_user_ids)
        self.assertTrue(first.stopped)  # 队首旧消息已被丢弃并接管

    async def test_worker_consumes_messages_serially(self) -> None:
        plugin = make_plugin(forward_queue_size=10)
        plugin._forward_queue = asyncio.Queue(maxsize=10)

        post_order: list[int] = []
        send_order: list[int] = []

        async def fake_post(payload: dict) -> tuple[str, dict]:
            post_order.append(payload["_seq"])
            await asyncio.sleep(0.01)
            return "reply", {}

        async def fake_send(event: Any, text: str, is_group: bool, user_id: str) -> None:
            send_order.append(user_id)

        plugin._post_to_omnibot = fake_post
        plugin._send_reply = fake_send

        task = asyncio.create_task(plugin._run_forward_worker())
        for seq in (1, 2, 3):
            event = FakeEvent("group", f"u{seq}")
            payload = {"_seq": seq, "message_type": "group", "user_id": seq, "group_id": 20001}
            plugin._enqueue_forward(event, payload)

        # 等 worker 全部处理完（串行：post_1 -> send_1 -> post_2 -> send_2 -> ...）
        deadline = asyncio.get_running_loop().time() + 2.0
        done = lambda: (
            post_order == [1, 2, 3] and send_order == ["1", "2", "3"]
        )
        while not done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        # 确认 worker 已退出（blanket drain 完成）
        if not done():
            await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        self.assertEqual([1, 2, 3], post_order)
        # 严格串行：必须 post_2 在 send_1 之后，post_3 在 send_2 之后
        self.assertEqual(["1", "2", "3"], send_order)

    def test_forward_queue_size_min_is_one(self) -> None:
        plugin = make_plugin(forward_queue_size=-5)
        self.assertEqual(1, plugin.forward_queue_size)

        plugin2 = make_plugin(forward_queue_size=0)
        self.assertEqual(1, plugin2.forward_queue_size)

    async def test_stop_worker_cancels_and_clears(self) -> None:
        plugin = self.dummy_plugin()
        plugin._forward_worker = asyncio.create_task(plugin._run_forward_worker())
        plugin._enqueue_forward(FakeEvent("group", "x"), {"user_id": 1})
        await asyncio.sleep(0)

        await plugin._stop_forward_worker()
        self.assertIsNone(plugin._forward_queue)
        self.assertTrue(
            plugin._forward_worker is None
            or plugin._forward_worker.done()
        )


if __name__ == "__main__":
    unittest.main()
