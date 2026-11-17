"""Public API paging, persistent delivery, and owner identity boundaries."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alice_monitor.client import Page, TiebaMonitorClient
from alice_monitor.service import STATE_KEY, MonitorService


def row(key: str, timestamp: int, floor: int = 0) -> dict:
    return {"key": key, "time": timestamp, "floor": floor, "title": f"Thread {key}",
            "text": "an update", "url": f"https://tieba.baidu.com/p/{key}"}


class FakePlugin:
    def __init__(self):
        self.data = {}
        self.context = SimpleNamespace(send_message=AsyncMock(return_value=True))

    async def get_kv_data(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    async def put_kv_data(self, key, value):
        self.data[key] = copy.deepcopy(value)


class FakeClient:
    def __init__(self):
        self.user_pages = {1: Page([row("100", 100)], True), 2: Page([], False)}
        self.thread_pages = {1: Page([row("100", 100, 1)], False, 1, "123", "Title", "Alice")}
        self.calls = []

    async def user_page(self, target, page):
        self.calls.append(("user", page))
        return copy.deepcopy(self.user_pages[page])

    async def thread_page(self, target, page):
        self.calls.append(("thread", page))
        return copy.deepcopy(self.thread_pages[page])

    async def close(self):
        pass


def service(plugin=None, client=None, send=None, **settings):
    return MonitorService(plugin or FakePlugin(), {"monitoring": {"enabled": True, **settings}},
                          client=client or FakeClient(), send=send or AsyncMock(return_value=True))


def test_baseline_no_history_and_failed_send_survives_restart():
    async def scenario():
        plugin, client, send = FakePlugin(), FakeClient(), AsyncMock(return_value=False)
        monitor = service(plugin, client, send)
        sub = await monitor.add_subscription("user", "123", "aiocqhttp:GroupMessage:456")
        await monitor.run_once()
        send.assert_not_awaited()
        client.user_pages[1] = Page([row("101", 110), row("100", 100)], True)
        await monitor.run_once(force=True)
        stored = plugin.data[STATE_KEY]["subscriptions"][sub["id"]]
        assert stored["watermark_time"] == 100
        assert [item["key"] for item in stored["pending"]] == ["101"]
        assert "确认" in stored["last_error"]
        recovered_send = AsyncMock(return_value=True)
        recovered = service(plugin, client, recovered_send)
        await recovered.run_once(force=True)
        assert recovered_send.await_count == 1
        await recovered.run_once(force=True)
        assert recovered_send.await_count == 1
        stored = plugin.data[STATE_KEY]["subscriptions"][sub["id"]]
        assert stored["watermark_time"] == 110
        assert not stored["pending"]
        await monitor.close()
        await recovered.close()
    asyncio.run(scenario())


def test_user_later_pages_are_not_lost_after_first_page_ack():
    async def scenario():
        client, send = FakeClient(), AsyncMock(return_value=True)
        monitor = service(client=client, send=send, max_pages_per_check=2)
        await monitor.add_subscription("user", "123", "aiocqhttp:GroupMessage:456")
        await monitor.run_once()
        client.user_pages = {1: Page([row("104", 140), row("103", 130)], True),
                             2: Page([row("102", 120)], True),
                             3: Page([row("101", 110), row("100", 100)], True),
                             4: Page([], False)}
        await monitor.run_once(force=True)
        await monitor.run_once(force=True)
        assert send.await_count == 4
        # A deletion while paging can shift an older, unseen new item onto a
        # page already read. The next full sweep must still find it.
        client.user_pages = {1: Page([row("104", 140), row("105", 125)], True),
                             2: Page([], False)}
        await monitor.run_once(force=True)
        assert send.await_count == 5
        assert "/p/105" in send.await_args_list[-1].args[1]
        pushed = "\n".join(call.args[1] for call in send.await_args_list)
        for key in ("101", "102", "103", "104"):
            assert f"/p/{key}" in pushed
        await monitor.run_once(force=True)
        assert send.await_count == 5
        await monitor.close()
    asyncio.run(scenario())


def test_thread_uses_tail_anchor_and_rejects_changed_owner():
    async def scenario():
        client, send = FakeClient(), AsyncMock(return_value=True)
        client.thread_pages = {i: Page([row(str(100 + i), 100, i * 30)], i < 10, 10,
                                      "123", "Long thread", "Alice") for i in range(1, 11)}
        monitor = service(client=client, send=send)
        await monitor.add_subscription("thread", "987", "aiocqhttp:GroupMessage:456")
        await monitor.run_once()
        assert client.calls == [("thread", 1), ("thread", 10)]
        client.calls.clear()
        client.thread_pages[10].items.append(row("999", 110, 301))
        await monitor.run_once(force=True)
        assert client.calls == [("thread", 1), ("thread", 9), ("thread", 10)]
        assert send.await_count == 1
        client.thread_pages[9].owner_id = "999"
        await monitor.run_once(force=True)
        assert send.await_count == 1
        assert "楼主" in (await monitor.list_subscriptions())[0]["last_error"]
        await monitor.close()
    asyncio.run(scenario())


def test_follow_author_link_and_pause_do_not_send():
    async def scenario():
        client, send = FakeClient(), AsyncMock(return_value=True)
        monitor = service(client=client, send=send)
        sub = await monitor.add_subscription("user", "https://tieba.baidu.com/p/987",
                                             "aiocqhttp:GroupMessage:456")
        assert sub["target"] == "123"
        assert sub["name"] == "Alice"
        await monitor.run_once()
        await monitor.update_subscription(sub["id"], {"enabled": False})
        client.user_pages[1] = Page([row("101", 110)], False)
        await monitor.run_once(force=True)
        send.assert_not_awaited()
        await monitor.update_subscription(sub["id"], {"enabled": True})
        await monitor.run_once(force=True)
        assert send.await_count == 1
        await monitor.close()
    asyncio.run(scenario())


def test_live_userpost_shape_and_original_poster_only():
    async def scenario():
        client = TiebaMonitorClient()
        client._request = AsyncMock(return_value={"error_code": "0", "hide_post": "0", "post_list": [
            {"thread_id": "11180655858", "is_thread": "1", "user_id": "595970344",
             "create_time": "1794203153", "title": "正在尝试复活", "name_show": "Adnini983",
             "first_post_content": [{"type": "0", "text": "正文"}]}
        ]})
        page = await client.user_page("595970344", 1)
        assert page.has_more  # rn=20 is not a reliable count or last-page marker.
        assert page.items[0]["text"] == "正文"
        assert page.name == "Adnini983"
        client._request.return_value["hide_post"] = "1"
        with pytest.raises(ValueError, match="隐私"):
            await client.user_page("595970344", 1)
        client._request.return_value = {"thread": {"id": "987", "author": {"id": "123"}},
            "page": {"total_page": 1}, "post_list": [
                {"id": "1", "floor": 1, "author_id": "123", "time": 100, "content": [{"type": 0, "text": "主楼"}]},
                {"id": "2", "floor": 2, "author_id": "999", "time": 110, "content": [{"type": 0, "text": "他人回复"}]},
                {"id": "3", "floor": 3, "author_id": "123", "time": 120, "content": [{"type": 0, "text": "楼主更新"}]},
            ]}
        page = await client.thread_page("987", 1)
        assert [item["floor"] for item in page.items] == [1, 3]
        await client.close()
    asyncio.run(scenario())
