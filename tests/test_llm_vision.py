"""Visual inputs retain provenance, obey byte limits and never send messages."""

import asyncio
import base64
import io
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from PIL import Image

from alice_tieba_core import llm_vision as vision
from alice_tieba_core.downloader import security


URL = "https://tiebapic.baidu.com/forum/pic/item/"
POST = "https://tieba.baidu.com/p/12345678"


def png(*, size=(80, 40), color="red", mode="RGB"):
    output = io.BytesIO()
    Image.new(mode, size, color).save(output, "PNG")
    return output.getvalue()


def groups(*names):
    return [{"candidates": [{"url": URL + name, "format": "png"}]} for name in names]


def payload(*names, comments=None):
    result = {"source_url": POST, "status": "ok", "images": groups(*names)}
    if comments is not None:
        result["hot_comments"] = comments
    return {"results": [result]}


class Response:
    def __init__(self, data=b"", *, status=200, headers=None, url=None, block=None):
        self.data = data
        self.status = status
        self.headers = headers or {"Content-Type": "image/png"}
        self.url = url or URL + "image.png"
        self.content = self
        self.block = block
        self.closed = False
        self.reads = 0

    async def iter_chunked(self, size):
        if self.block is not None:
            self.block.set()
            await asyncio.Future()
        for offset in range(0, len(self.data), size):
            self.reads += 1
            yield self.data[offset:offset + size]

    def close(self):
        self.closed = True

    def release(self):
        self.closed = True


def fake_network(monkeypatch, responses):
    state = SimpleNamespace(connector_closed=False, sessions=[], connector_options=[], requests=[])

    class Connector:
        _media_public_only_resolver = object.__new__(security.PublicOnlyResolver)

        async def close(self):
            state.connector_closed = True

    def connector(**kwargs):
        state.connector_options.append(kwargs)
        return Connector()

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]
            self.options = kwargs
            self.closed = False
            state.sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.closed = True
            await self.connector.close()

        async def request(self, method, url, **kwargs):
            state.requests.append((method, url, kwargs))
            return responses[url] if isinstance(responses, dict) else responses[len(state.requests) - 1]

    monkeypatch.setattr(vision, "create_public_only_connector", connector)
    monkeypatch.setattr(vision.aiohttp, "ClientSession", Session)

    async def request(session, method, url, **kwargs):
        return await session.request(method, url, **kwargs)

    monkeypatch.setattr(vision, "safe_request", request)
    return state


def test_main_and_reply_inputs_have_independent_floor_labels_and_no_credentials(monkeypatch):
    responses = {URL + name: Response(png(color=color)) for name, color in (("main.png", "red"), ("reply.png", "blue"))}
    state = fake_network(monkeypatch, responses)
    raw = payload("main.png", comments=[{"floor": 9, "comment_id": "909", "images": groups("reply.png")}])

    images, report = asyncio.run(vision.prepare_vision_images(raw))

    assert len(images) == 2 and report["status"] == "ok"
    assert images[0].source_ref["scope"] == "main" and images[0].source_ref["floor"] == 1
    assert images[1].source_ref["floor"] == 9 and images[1].source_ref["comment_id"] == "909"
    assert "第 9 楼" in images[1].label
    assert all(image.mime_type == "image/jpeg" for image in images)
    assert all(Image.open(io.BytesIO(base64.b64decode(image.data))).size == (80, 40) for image in images)
    assert state.connector_closed and all(session.closed for session in state.sessions)
    for _, _, kwargs in state.requests:
        assert set(kwargs["headers"]) == {"User-Agent", "Referer"}
        assert "Cookie" not in kwargs["headers"]
    assert all(response.closed for response in responses.values())


def test_image_budget_interleaves_main_and_reply_and_reports_omission(monkeypatch):
    response = Response(png())
    fake_network(monkeypatch, {URL + name: response for name in ("main-1.png", "main-2.png", "main-3.png", "reply.png")})
    raw = payload("main-1.png", "main-2.png", "main-3.png", comments=[{"floor": 4, "images": groups("reply.png")}])

    images, report = asyncio.run(vision.prepare_vision_images(raw, max_images=2))

    assert [item.source_ref["scope"] for item in images] == ["main", "comment"]
    assert len(report["skipped"]) == 2 and all(item["reason"] == "image_budget" for item in report["skipped"])
    assert report["status"] == "partial"


def test_repeated_image_is_downloaded_once_and_keeps_all_sources(monkeypatch):
    state = fake_network(monkeypatch, {URL + "same.png": Response(png())})
    raw = payload("same.png", comments=[{"floor": 3, "images": groups("same.png")}])

    images, report = asyncio.run(vision.prepare_vision_images(raw))

    assert len(images) == 1 and len(state.requests) == 1
    assert [ref["floor"] for ref in images[0].source_refs] == [1, 3]
    assert len(report["images"][0]["source_refs"]) == 2


def test_only_metadata_images_are_collected_not_urls_in_body_or_private_inputs(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(vision, "create_public_only_connector", create)
    raw = {"results": [{"desc": URL + "body.png", "images": [
        {"candidates": [{"url": "http://127.0.0.1/private.png"}]},
        {"candidates": [{"url": "https://attacker.example/image.png"}]},
        {"candidates": [{"url": URL + "secret.png?BDUSS=secret"}]},
    ]}]}

    images, report = asyncio.run(vision.prepare_vision_images(raw))

    assert images == [] and report["status"] == "none"
    create.assert_not_called()


def test_redirect_to_private_address_is_rejected_before_second_request(monkeypatch):
    first = Response(status=302, headers={"Location": "http://127.0.0.1/private.png"}, url=URL + "redirect.png")
    state = fake_network(monkeypatch, [first])
    original_validation = security.validate_remote_url

    async def validate(url):
        if urlsplit(url).hostname == "tiebapic.baidu.com":
            return
        await original_validation(url)

    monkeypatch.setattr(security, "validate_remote_url", validate)
    monkeypatch.setattr(vision, "safe_request", security.safe_request)

    images, report = asyncio.run(vision.prepare_vision_images(payload("redirect.png")))

    assert images == [] and report["skipped"][0]["reason"] == "unsafe_url"
    assert len(state.requests) == 1 and first.closed and state.connector_closed
    assert state.requests[0][2]["allow_redirects"] is False


@pytest.mark.parametrize("declared", [True, False])
def test_declared_or_streamed_oversized_images_are_closed_and_not_decoded(monkeypatch, declared):
    headers = {"Content-Type": "image/png"}
    if declared:
        headers["Content-Length"] = "1000"
    response = Response(b"X" * 1000, headers=headers)
    state = fake_network(monkeypatch, {URL + "large.png": response})
    decode = AsyncMock()
    monkeypatch.setattr(vision, "_decode_image", decode)

    images, report = asyncio.run(vision.prepare_vision_images(payload("large.png"), max_image_bytes=128))

    assert images == [] and report["skipped"][0]["reason"] == "too_large"
    assert response.closed and state.connector_closed
    decode.assert_not_awaited()
    if declared:
        assert response.reads == 0


def test_total_download_budget_is_shared_by_concurrent_inputs(monkeypatch):
    random_image = Image.effect_noise((180, 180), 64).convert("RGB")
    buffer = io.BytesIO()
    random_image.save(buffer, "PNG")
    data = buffer.getvalue()
    monkeypatch.setattr(vision, "MAX_TOTAL_BYTES", len(data) + 10)
    responses = {URL + name: Response(data) for name in ("one.png", "two.png")}
    fake_network(monkeypatch, responses)

    images, report = asyncio.run(vision.prepare_vision_images(payload("one.png", "two.png")))

    assert len(images) == 1 and report["status"] == "partial"
    assert report["downloaded_bytes"] <= len(data) + 10
    assert report["skipped"][0]["reason"] == "byte_budget"
    assert all(response.closed for response in responses.values())


def test_image_decode_limits_pixels_and_invalid_images(monkeypatch):
    monkeypatch.setattr(vision, "MAX_IMAGE_PIXELS", 3)
    response = Response(png(size=(2, 2)))
    fake_network(monkeypatch, {URL + "pixels.png": response})

    images, report = asyncio.run(vision.prepare_vision_images(payload("pixels.png")))

    assert images == [] and report["skipped"][0]["reason"] == "pixels_exceeded"
    with pytest.raises(vision._VisionFailure, match="invalid_image"):
        vision._prepare_image(b"untrusted non-image data")


def test_exif_transparency_and_animation_are_processed_without_changing_original():
    original = Image.new("RGB", (30, 50), "red")
    exif = Image.Exif()
    exif[274] = 6
    encoded = io.BytesIO()
    original.save(encoded, "JPEG", exif=exif)
    jpeg = encoded.getvalue()
    data, mime, width, height, _, _ = vision._prepare_image(jpeg)
    assert (width, height) == (50, 30) and mime == "image/jpeg"
    assert Image.open(io.BytesIO(data)).size == (50, 30)

    rgba = png(mode="RGBA", color=(255, 0, 0, 0))
    data, mime, *_ = vision._prepare_image(rgba)
    assert mime == "image/png"
    assert Image.open(io.BytesIO(data)).getpixel((0, 0))[3] == 0

    first = Image.new("RGB", (20, 20), "red")
    second = Image.new("RGB", (20, 20), "blue")
    animated = io.BytesIO()
    first.save(animated, "GIF", save_all=True, append_images=[second], duration=20, loop=0)
    data, _, _, _, first_frame, _ = vision._prepare_image(animated.getvalue())
    assert first_frame
    assert Image.open(io.BytesIO(data)).convert("RGB").getpixel((0, 0))[0] > 200


def test_image_edge_and_encoded_output_are_bounded(monkeypatch):
    data, _, width, height, _, resized = vision._prepare_image(png(size=(1800, 1000)))
    assert max(width, height) <= 1600 and resized
    assert len(data) <= vision.MAX_OUTPUT_IMAGE_BYTES
    monkeypatch.setattr(vision, "MAX_OUTPUT_IMAGE_BYTES", 10)
    with pytest.raises(vision._VisionFailure, match="output_budget"):
        vision._prepare_image(png())


def test_candidate_failure_can_use_alternative_source(monkeypatch):
    responses = {URL + "bad.png": Response(b"not an image"), URL + "good.png": Response(png())}
    fake_network(monkeypatch, responses)
    raw = {"results": [{"source_url": POST, "images": [{"candidates": [
        {"url": URL + "bad.png"}, {"url": URL + "good.png"},
    ]}]}]}

    images, report = asyncio.run(vision.prepare_vision_images(raw))

    assert len(images) == 1 and images[0].source_url.endswith("good.png")
    assert report["status"] == "ok"


def test_configured_proxy_is_explicit_and_environment_proxy_is_not_used(monkeypatch):
    state = fake_network(monkeypatch, {URL + "image.png": Response(png())})
    proxy = "http://127.0.0.1:7890"

    asyncio.run(vision.prepare_vision_images(payload("image.png"), proxy_url=proxy, trusted_proxy_urls=[proxy]))

    assert state.connector_options[0]["trusted_proxy_urls"] == [proxy]
    assert state.sessions[0].options["trust_env"] is False
    assert state.requests[0][2]["proxy"] == proxy
    assert isinstance(state.sessions[0].options["cookie_jar"], vision.aiohttp.DummyCookieJar)


def test_disabled_visual_inputs_create_no_session(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(vision, "create_public_only_connector", create)
    images, report = asyncio.run(vision.prepare_vision_images(payload("image.png"), max_images=0))
    assert images == [] and report["status"] == "disabled"
    create.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1])
def test_expired_tool_budget_does_not_start_an_extra_visual_download(monkeypatch, timeout):
    create = AsyncMock()
    monkeypatch.setattr(vision, "create_public_only_connector", create)
    images, report = asyncio.run(vision.prepare_vision_images(payload("image.png"), timeout_seconds=timeout))
    assert images == [] and report["status"] == "deadline_exceeded"
    create.assert_not_called()


def test_whole_stage_timeout_closes_response_and_owned_session(monkeypatch):
    async def run():
        entered = asyncio.Event()
        response = Response(block=entered)
        state = fake_network(monkeypatch, {URL + "timeout.png": response})
        images, report = await vision.prepare_vision_images(payload("timeout.png"), timeout_seconds=0.02)
        assert entered.is_set() and images == []
        assert report["skipped"][0]["reason"] == "timeout"
        assert response.closed and state.connector_closed
    asyncio.run(run())


def test_cancellation_drains_decode_worker_then_closes_session(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original = vision._prepare_image

    def block(data):
        entered.set()
        release.wait(2)
        return original(data)

    monkeypatch.setattr(vision, "_prepare_image", block)
    state = fake_network(monkeypatch, {URL + "image.png": Response(png())})

    async def run():
        task = asyncio.create_task(vision.prepare_vision_images(payload("image.png")))
        while not entered.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state.connector_closed and state.sessions[0].closed
    asyncio.run(run())
