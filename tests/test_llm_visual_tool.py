"""The public tool returns actual images without sending chat messages."""

import asyncio
import base64
import importlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image

from plugin_test_support import FakeEvent, TIEBA_URL, fake_network, make_plugin


def setup_vision(monkeypatch, plugin):
    metadata = {"url": TIEBA_URL, "title": "帖子", "desc": "主楼正文", "platform": "tieba",
                "image_urls": [["https://tiebapic.baidu.com/main.png"]],
                "hot_comments": [{"floor": 2, "comment_id": "222", "message": "图片回复",
                                  "image_urls": [["https://tiebapic.baidu.com/reply.png"]]}]}
    parse, connector, _ = fake_network(monkeypatch, plugin, metadata, all_instances=True)
    module = importlib.import_module(type(plugin.llm_content_fetcher).__module__)
    monkeypatch.setattr(module, "create_public_only_connector", lambda **_: connector)
    stream = io.BytesIO()
    Image.new("RGB", (8, 6), "blue").save(stream, "PNG")
    data = base64.b64encode(stream.getvalue()).decode()
    frames = [SimpleNamespace(data=data, mime_type="image/png", label=label) for label in
              ("帖子 1 · 主楼 · 图片 1", "帖子 1 · 第 2 楼 · 图片 1")]
    prepared = AsyncMock(return_value=(frames, {"status": "ok", "prepared": 2, "skipped": []}))
    monkeypatch.setattr(module, "prepare_vision_images", prepared)
    return parse, prepared, frames


def test_tool_attaches_main_and_reply_images_and_keeps_original_urls(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, llm_tool={"max_images": 2})
        _, prepared, frames = setup_vision(monkeypatch, plugin)
        event = FakeEvent()
        tool = plugin.context.add_llm_tools.call_args.args[0]
        result = await tool.call(SimpleNamespace(context=SimpleNamespace(event=event)),
                                 url=TIEBA_URL, max_images=24)
        assert isinstance(result, CallToolResult)
        assert isinstance(result.content[0], TextContent)
        content = json.loads(result.content[0].text)
        assert content["results"][0]["images"][0]["candidates"][0]["url"].endswith("/main.png")
        assert content["results"][0]["hot_comments"][0]["images"][0]["candidates"][0]["url"].endswith("/reply.png")
        assert content["results"][0]["hot_comments"][0]["floor"] == 2
        assert [item.data for item in result.content if isinstance(item, ImageContent)] == [frame.data for frame in frames]
        assert "主楼" in result.content[1].text and "第 2 楼" in result.content[3].text
        assert prepared.await_args.kwargs["max_images"] == 2
        assert 0 < prepared.await_args.kwargs["timeout_seconds"] <= 45
        event.send.assert_not_awaited()
        assert not plugin.llm_content_fetcher._tasks
        assert len(plugin.context.add_llm_tools.call_args.args) == 1
        await plugin.terminate()
    asyncio.run(run())


@pytest.mark.parametrize("arguments, settings", [
    ({"include_images": False}, {}), ({"max_images": 0}, {}),
    ({"include_media": False}, {}), ({}, {"image_vision": False}),
])
def test_image_switches_never_download_or_send(tmp_path, monkeypatch, arguments, settings):
    async def run():
        plugin = make_plugin(tmp_path, llm_tool=settings)
        _, prepared, _ = setup_vision(monkeypatch, plugin)
        event = FakeEvent()
        result = await plugin.llm_content_fetcher.fetch_tool(event, url=TIEBA_URL, **arguments)
        assert isinstance(result, str)
        assert json.loads(result)["results"][0]["desc"] == "主楼正文"
        prepared.assert_not_awaited()
        event.send.assert_not_awaited()
        await plugin.terminate()
    asyncio.run(run())


def test_image_channel_failure_preserves_text_and_links(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path)
        _, prepared, _ = setup_vision(monkeypatch, plugin)
        prepared.side_effect = OSError("private request error")
        output = await plugin.llm_content_fetcher.fetch_tool(FakeEvent(), url=TIEBA_URL)
        result = json.loads(output)
        assert result["results"][0]["desc"] == "主楼正文"
        assert result["results"][0]["images"]
        assert result["vision"]["status"] == "unavailable"
        assert "private request error" not in output
        await plugin.terminate()
    asyncio.run(run())


def test_unload_cancels_vision_work_and_concurrency_counts_it(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, llm_tool={"max_concurrent_calls": 1})
        _, prepared, _ = setup_vision(monkeypatch, plugin)
        entered = asyncio.Event()
        async def blocked(*_, **__):
            entered.set()
            await asyncio.Future()
        prepared.side_effect = blocked
        event = FakeEvent()
        task = asyncio.create_task(plugin.llm_content_fetcher.fetch_tool(event, url=TIEBA_URL))
        await asyncio.wait_for(entered.wait(), 2)
        result = await plugin.llm_content_fetcher.fetch_tool(event, url=TIEBA_URL)
        assert json.loads(result)["error"]["code"] == "busy"
        await plugin.terminate()
        with pytest.raises(asyncio.CancelledError):
            await task
        event.send.assert_not_awaited()
        assert not plugin.llm_content_fetcher._tasks
    asyncio.run(run())
