from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


PLUGIN_MAIN = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location(
    "omnibot_plugin_panel_lifecycle_test",
    PLUGIN_MAIN,
)
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


class CommandPanelLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, **config):
        plugin = object.__new__(OMNIBOT.OmniBotGateway)
        plugin.config = config
        plugin.logger = FakeLogger()
        plugin._panel_sync_task = None
        return plugin

    async def test_plugin_loaded_starts_panel_sync_after_hot_reload(self):
        plugin = self.make_plugin(enabled=True)
        calls: list[str] = []

        plugin._start_forward_worker = lambda: calls.append("forward")
        plugin._start_command_panel_sync = lambda: calls.append("panel")

        await plugin.on_plugin_loaded()

        self.assertEqual(["forward", "panel"], calls)

    async def test_plugin_loaded_does_nothing_when_disabled(self):
        plugin = self.make_plugin(enabled=False)
        calls: list[str] = []

        plugin._start_forward_worker = lambda: calls.append("forward")
        plugin._start_command_panel_sync = lambda: calls.append("panel")

        await plugin.on_plugin_loaded()

        self.assertEqual([], calls)

    def test_panel_sync_skip_reasons_are_logged(self):
        plugin = self.make_plugin(
            enabled=False,
            command_panel_sync_enabled=False,
            omnibot_site_url="",
            qq_bot_secret="",
        )

        plugin._start_command_panel_sync()

        self.assertEqual("warning", plugin.logger.messages[0][0])
        self.assertIn("插件未启用", plugin.logger.messages[0][1])
        self.assertIn("未开启“同步 QQ 官方指令面板”", plugin.logger.messages[0][1])
        self.assertIn("未配置 OmniBot 站点地址", plugin.logger.messages[0][1])
        self.assertIn("未配置 QQ 安全密钥", plugin.logger.messages[0][1])


if __name__ == "__main__":
    unittest.main()
