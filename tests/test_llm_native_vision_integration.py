"""The real AstrBot runner consumes tool images and its native sender reuses them."""

import asyncio
import base64
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image as PILImage

from astrbot.api.message_components import Image
from astrbot.core import astr_agent_tool_exec as executor_module
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import ImageURLPart
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners import tool_loop_agent_runner as runner_module
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.tools import message_tools

from alice_tieba_core.llm_tool import CoreAliceTiebaFetchContentTool
from plugin_test_support import FakeEvent


def _png(color):
    output = io.BytesIO()
    PILImage.new("RGB", (12, 8), color).save(output, "PNG")
    return output.getvalue()


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(getattr(part, "text", "") for part in content or [])


class LocalExecutorFixture:
    """Keep fixture dispatch small while using AstrBot's actual local executor."""

    def execute(self, tool, run_context, **kwargs):
        return executor_module.FunctionToolExecutor._execute_local(
            tool, run_context, **kwargs,
        )


@pytest.mark.parametrize("supports_images", [True, False])
def test_native_runner_places_images_in_provider_context_and_sender_reuses_cache(
    tmp_path, monkeypatch, supports_images,
):
    async def run():
        cache_directory = tmp_path / "astrbot-temp" / "tool_images"
        cache_directory.mkdir(parents=True)
        monkeypatch.setattr(runner_module.tool_image_cache, "_cache_dir", str(cache_directory))
        monkeypatch.setattr(message_tools, "get_astrbot_temp_path", lambda: str(cache_directory.parent))
        first, second = _png("red"), _png("blue")
        result_json = {
            "status": "ok", "results": [{"source_url": "https://tieba.baidu.com/p/1"}],
            "visual_images": [{"origin": "main", "index": 1}, {"origin": "comment", "floor": 2, "index": 1}],
        }
        tool_result = CallToolResult(content=[
            TextContent(type="text", text=json.dumps(result_json)),
            TextContent(type="text", text="Main floor, original image 1"),
            ImageContent(type="image", data=base64.b64encode(first).decode(), mimeType="image/png"),
            TextContent(type="text", text="Floor 2, original image 1"),
            ImageContent(type="image", data=base64.b64encode(second).decode(), mimeType="image/png"),
        ])
        fetcher = SimpleNamespace(fetch_tool=AsyncMock(return_value=tool_result))
        tool = CoreAliceTiebaFetchContentTool(fetcher=fetcher)
        event = FakeEvent()
        host = SimpleNamespace(
            send_message=AsyncMock(return_value=True),
            get_config=lambda **_: {"provider_settings": {"computer_use_runtime": "none"}},
        )
        context = ContextWrapper(context=SimpleNamespace(event=event, context=host))
        request = ProviderRequest(prompt="Read the main floor and floor 2 images.", func_tool=ToolSet([tool]))
        provider_requests = []
        responses = [
            LLMResponse(
                role="assistant", tools_call_name=[tool.name], tools_call_ids=["tieba-tool-call"],
                tools_call_args=[{"url": "https://tieba.baidu.com/p/1", "include_images": True}],
            ),
            LLMResponse(role="assistant", completion_text="Reviewed the available tool content."),
        ]

        async def text_chat(**payload):
            provider_requests.append(copy.deepcopy(payload["contexts"]))
            return responses.pop(0)

        modalities = ["text", "tool_use"] + (["image"] if supports_images else [])
        provider = SimpleNamespace(
            provider_config={"id": "vision-fixture", "modalities": modalities, "max_context_tokens": 0},
            text_chat=text_chat,
        )
        runner = runner_module.ToolLoopAgentRunner()
        await runner.reset(
            provider=provider, request=request, run_context=context,
            tool_executor=LocalExecutorFixture(), agent_hooks=BaseAgentRunHooks(),
        )

        first_step = [response async for response in runner.step()]
        assert any(response.type == "tool_call" for response in first_step)
        fetcher.fetch_tool.assert_awaited_once_with(
            event, url="https://tieba.baidu.com/p/1", include_images=True,
        )
        event.send.assert_not_awaited()
        host.send_message.assert_not_awaited()
        cache_paths = sorted(cache_directory.glob("*.png"))
        assert len(cache_paths) == 2
        assert [path.read_bytes() for path in cache_paths] == [first, second]
        tool_text = "\n".join(_text(message.content) for message in context.messages if message.role == "tool")
        assert "https://tieba.baidu.com/p/1" in tool_text
        assert "send_message_to_user" in tool_text
        assert all(str(path) in tool_text for path in cache_paths)
        image_parts = [
            part for message in context.messages if message.role == "user"
            for part in message.content if isinstance(part, ImageURLPart)
        ]
        assert len(image_parts) == (2 if supports_images else 0)
        if supports_images:
            assert [part.image_url.url for part in image_parts] == [
                "data:image/png;base64," + base64.b64encode(data).decode() for data in (first, second)
            ]
            assert [part.image_url.id for part in image_parts] == [str(path) for path in cache_paths]

        _ = [response async for response in runner.step()]
        assert len(provider_requests) == 2 and runner.done()
        delivered_images = [
            part for message in provider_requests[-1] if message["role"] == "user"
            for part in message["content"] if isinstance(part, dict) and part.get("type") == "image_url"
        ]
        assert len(delivered_images) == (2 if supports_images else 0)

        native_sender = message_tools.SendMessageToUserTool()
        sent = await native_sender.call(context, messages=[
            {"type": "image", "path": str(path)} for path in cache_paths
        ])
        assert "Message sent" in sent
        host.send_message.assert_awaited_once()
        destination, message_chain = host.send_message.await_args.args
        assert str(destination) == event.unified_msg_origin
        assert len(message_chain.chain) == 2
        for component, path in zip(message_chain.chain, cache_paths):
            assert isinstance(component, Image)
            assert str(path).replace("\\", "/") in str(component.file).replace("\\", "/")
            assert path.is_file()
        event.send.assert_not_awaited()

    asyncio.run(run())
