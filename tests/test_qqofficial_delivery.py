"""QQ 官方图库保持卡片和原图分开，并避免 Nodes / 不确定结果重发。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Image, Nodes, Plain, Video

from plugin_test_support import FakeEvent, make_plugin


@pytest.mark.parametrize("aggregated", [False, True])
def test_qqofficial_sends_card_then_one_original_gallery(tmp_path, monkeypatch, aggregated):
    async def run():
        plugin = make_plugin(tmp_path)
        from astrbot_plugin_alice_tieba.alice_tieba_core.message_adapter import qqofficial

        calls = []
        event = FakeEvent(platform="qq_official")
        event.send = AsyncMock(side_effect=lambda chain: calls.append(("regular", chain)))
        gallery = AsyncMock(side_effect=lambda _event, images: calls.append(("gallery", images)))
        monkeypatch.setattr(qqofficial, "send_qqofficial_gallery", gallery)
        card = Image.fromURL("https://example.org/card.png")
        originals = [Image.fromURL(f"https://example.org/{i}.png") for i in (1, 2)]
        metadata = {"card_node": card, "card_mode": "仅卡片", "media_nodes": originals,
                    "link_nodes": [card, *originals], "is_normal": True}
        if aggregated:
            await plugin.message_sender.send_aggregated_results(event, [metadata], "bot", "999")
        else:
            await plugin.message_sender.send_individual_results(event, [metadata["link_nodes"]], [metadata])
        assert calls == [("regular", [card]), ("gallery", originals)]
        assert not any(isinstance(component, Nodes) for _, chain in calls for component in chain)
        await plugin.terminate()
    asyncio.run(run())


def test_gallery_failure_falls_back_to_each_original_without_resending_card(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path)
        module = __import__("astrbot_plugin_alice_tieba.alice_tieba_core.message_adapter.qqofficial", fromlist=[""])
        monkeypatch.setattr(module, "send_qqofficial_gallery", AsyncMock(side_effect=ValueError("unavailable")))
        images = [Image.fromURL(f"https://example.org/{i}.png") for i in (1, 2)]
        event = FakeEvent(platform="qq_official")
        await plugin.message_sender.send_individual_results(event, [images])
        assert [call.args[0] for call in event.send.await_args_list] == [[image] for image in images]
        await plugin.terminate()
    asyncio.run(run())


def test_uncertain_gallery_send_is_not_repeated_as_individual_images(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path)
        module = __import__("astrbot_plugin_alice_tieba.alice_tieba_core.message_adapter.qqofficial", fromlist=[""])
        monkeypatch.setattr(module, "send_qqofficial_gallery", AsyncMock(side_effect=module.QQOfficialGalleryError("timeout", may_have_been_sent=True)))
        images = [Image.fromURL(f"https://example.org/{i}.png") for i in (1, 2)]
        event = FakeEvent(platform="qq_official")
        await plugin.message_sender.send_individual_results(event, [images])
        sent = [component for call in event.send.await_args_list for component in call.args[0]]
        assert not any(isinstance(component, Image) for component in sent)
        assert any(isinstance(component, Plain) for component in sent)
        await plugin.terminate()
    asyncio.run(run())


def test_qqofficial_gallery_toggle_and_mixed_video_preserve_originals(tmp_path):
    async def run():
        plugin = make_plugin(tmp_path, message={"qqofficial_image_gallery": False})
        images = [Image.fromURL(f"https://example.org/{i}.png") for i in (1, 2)]
        video = Video.fromURL("https://example.org/video.mp4")
        event = FakeEvent(platform="qq_official")
        await plugin.message_sender.send_individual_results(event, [[images[0], video, images[1]]])
        sent = [component for call in event.send.await_args_list for component in call.args[0]]
        assert sent == [*images, video]
        await plugin.terminate()
    asyncio.run(run())
