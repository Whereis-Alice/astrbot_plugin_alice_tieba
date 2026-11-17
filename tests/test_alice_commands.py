"""Chat management never crosses session boundaries or escalates permissions."""

import asyncio
from unittest.mock import AsyncMock, Mock

from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import star_handlers_registry
from plugin_test_support import FakeEvent, main, make_plugin, sent_components


def test_registered_command_does_not_consume_parsing_keywords():
    handler = star_handlers_registry.get_handler_by_full_name(
        f"{main.__name__}_alice_tieba_command"
    )
    command = next(item for item in handler.event_filters if isinstance(item, CommandFilter))
    for text, expected in [
        ("贴吧", True), ("贴吧 会话", True), ("贴吧 关注 123", True),
        ("贴吧解析 https://tieba.baidu.com/p/1", False),
        ("解析贴吧 https://tieba.baidu.com/p/1", False), ("爱丽丝贴吧", False),
    ]:
        event = FakeEvent(text)
        event.is_at_or_wake_command = True
        event.get_message_str = lambda: text
        event.set_extra = Mock()
        assert command.filter(event, {}) is expected


def test_help_uses_commands_without_alice_prefix(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        event = FakeEvent("贴吧 帮助")
        await plugin.alice_tieba_command(event)
        text = sent_components(event)[0].text
        assert "贴吧 关注" in text and "解析贴吧" in text and "贴吧解析" in text
        assert "爱丽丝贴吧" not in text
        await plugin.terminate()
    asyncio.run(run())


def test_non_admin_cannot_create_subscription(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.monitor.list_subscriptions = AsyncMock()
        event = FakeEvent("贴吧 关注 123")
        await plugin.alice_tieba_command(event)
        plugin.monitor.list_subscriptions.assert_not_awaited()
        assert "仅管理员" in sent_components(event)[0].text
        await plugin.terminate()
    asyncio.run(run())


def test_admin_cannot_remove_another_session_subscription(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.monitor.list_subscriptions = AsyncMock(return_value=[
            {"id": "foreign", "session": "another:GroupMessage:777"},
        ])
        plugin.monitor.remove_subscription = AsyncMock()
        event = FakeEvent("贴吧 取消 foreign")
        event.is_admin = lambda: True
        await plugin.alice_tieba_command(event)
        plugin.monitor.remove_subscription.assert_not_awaited()
        assert "当前会话" in sent_components(event)[0].text
        await plugin.terminate()
    asyncio.run(run())


def test_send_false_is_preserved_for_monitor_retry(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.context.send_message = AsyncMock(return_value=False)
        assert await plugin._send_subscription_message("test:GroupMessage:1", "更新") is False
        await plugin.terminate()
    asyncio.run(run())


def test_check_all_queues_current_session_once_and_reports_disabled(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.monitor.list_subscriptions = AsyncMock(return_value=[
            {"id": "one", "session": "aiocqhttp:GroupMessage:333"},
            {"id": "two", "session": "aiocqhttp:GroupMessage:333"},
        ])
        plugin.monitor.check_now = AsyncMock(return_value={
            "queued": False, "message": "推送总开关已关闭",
        })
        event = FakeEvent("贴吧 检查")
        event.is_admin = lambda: True
        await plugin.alice_tieba_command(event)
        plugin.monitor.check_now.assert_awaited_once_with(
            None, session=event.unified_msg_origin,
        )
        assert sent_components(event)[0].text == "推送总开关已关闭"
        await plugin.terminate()
    asyncio.run(run())
