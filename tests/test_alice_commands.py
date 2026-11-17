"""Chat management never crosses session boundaries or escalates permissions."""

import asyncio
from unittest.mock import AsyncMock

from plugin_test_support import FakeEvent, make_plugin, sent_components


def test_non_admin_cannot_create_subscription(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path)
        plugin.monitor.list_subscriptions = AsyncMock()
        event = FakeEvent("爱丽丝贴吧 关注 123")
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
        event = FakeEvent("爱丽丝贴吧 取消 foreign")
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
        event = FakeEvent("爱丽丝贴吧 检查")
        event.is_admin = lambda: True
        await plugin.alice_tieba_command(event)
        plugin.monitor.check_now.assert_awaited_once_with(
            None, session=event.unified_msg_origin,
        )
        assert sent_components(event)[0].text == "推送总开关已关闭"
        await plugin.terminate()
    asyncio.run(run())
