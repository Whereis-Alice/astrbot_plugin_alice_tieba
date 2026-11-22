"""Native QQ galleries keep originals separate and observe upload ownership."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Image
from PIL import Image as PILImage

from alice_tieba_core.message_adapter import qqofficial as gallery


def picture(tmp_path, name, *, size=(100, 200), color="red"):
    path = tmp_path / name
    PILImage.new("RGB", size, color).save(path, "PNG")
    return Image.fromFileSystem(str(path)), path


def fake_event(*, private=False):
    request = AsyncMock()
    api = SimpleNamespace(
        _http=SimpleNamespace(request=request),
        post_group_message=AsyncMock(return_value={"id": "group-message"}),
        post_c2c_message=AsyncMock(return_value={"id": "private-message"}),
    )
    raw = SimpleNamespace(author=SimpleNamespace(user_openid="current-user"))
    if not private:
        raw.group_openid = "current-group"
    event = SimpleNamespace(
        bot=SimpleNamespace(api=api),
        message_obj=SimpleNamespace(raw_message=raw, message_id="source-message"),
        get_platform_name=lambda: "qq_official",
    )
    return event, api


def successful_materials(api):
    count = 0
    async def respond(route, **kwargs):
        nonlocal count
        if route.path.endswith("/prepare"):
            count += 1
            return {"upload_id": f"upload-{count}",
                    "presigned_url": "https://bucket.cos.ap-guangzhou.myqcloud.com/image"}
        if route.path.endswith("/complete"):
            identifier = kwargs["json"]["upload_id"]
            return {"image_id": identifier, "url": f"qqbot-media://image/v1/{identifier}"}
        assert route.method == "DELETE"
        return {}
    api._http.request.side_effect = respond


def fake_put(monkeypatch):
    async def put(_url, image):
        return image.md5
    mock = AsyncMock(side_effect=put)
    monkeypatch.setattr(gallery, "_put_image", mock)
    return mock


def test_local_originals_use_one_native_message_and_release_own_materials(tmp_path, monkeypatch):
    first, first_path = picture(tmp_path, "first.png", color="red")
    second, second_path = picture(tmp_path, "second.png", color="blue")
    card, card_path = picture(tmp_path, "card.png", color="green")
    originals = [first_path.read_bytes(), second_path.read_bytes()]
    event, api = fake_event()
    successful_materials(api)
    put = fake_put(monkeypatch)

    receipt = asyncio.run(gallery.send_qqofficial_gallery(event, [first, second]))

    assert receipt.image_count == 2 and receipt.message_id == "group-message"
    api.post_group_message.assert_awaited_once()
    payload = api.post_group_message.await_args.kwargs
    assert payload["group_openid"] == "current-group" and payload["msg_type"] == 2
    assert payload["msg_id"] == "source-message" and "media" not in payload
    markdown = payload["markdown"]["content"]
    assert markdown.count("![原图 ") == 2
    assert "qqbot-media://image/v1/upload-1" in markdown
    assert "qqbot-media://image/v1/upload-2" in markdown
    assert "#100px #200px" in markdown
    assert card.file not in markdown and card_path.exists()
    prepares = [call.kwargs["json"] for call in api._http.request.await_args_list
                if call.args[0].path.endswith("/prepare")]
    assert len(prepares) == 2
    for parameters, original in zip(prepares, originals):
        assert parameters["content_type"] == "image/png"
        assert parameters["file_name"].endswith(".png")
        assert parameters["file_size"] == len(original)
        assert parameters["md5"] == hashlib.md5(original, usedforsecurity=False).hexdigest()
        assert parameters["sha1"] == hashlib.sha1(original, usedforsecurity=False).hexdigest()
    assert put.await_count == 2
    deletes = [call.args[0].path for call in api._http.request.await_args_list
               if call.args[0].method == "DELETE"]
    assert deletes == ["/v2/media-images/upload-1/delete", "/v2/media-images/upload-2/delete"]
    assert first_path.read_bytes() == originals[0] and second_path.read_bytes() == originals[1]


def test_public_originals_need_no_upload_or_template(monkeypatch):
    event, api = fake_event(private=True)
    validate = AsyncMock()
    monkeypatch.setattr(gallery, "validate_remote_url", validate)
    images = [Image.fromURL("https://tiebapic.baidu.com/a(1).png"),
              Image.fromURL("https://tiebapic.baidu.com/b.png")]

    receipt = asyncio.run(gallery.send_qqofficial_gallery(event, images))

    assert receipt.message_id == "private-message"
    api.post_c2c_message.assert_awaited_once()
    api.post_group_message.assert_not_awaited()
    api._http.request.assert_not_awaited()
    payload = api.post_c2c_message.await_args.kwargs
    assert payload["openid"] == "current-user" and payload["msg_type"] == 2
    assert "custom_template_id" not in payload["markdown"]
    assert "a%281%29.png" in payload["markdown"]["content"]
    assert payload["markdown"]["content"].count("![原图 ") == 2
    assert validate.await_count == 2


def test_partial_upload_failure_cleans_successful_material_before_fallback(tmp_path, monkeypatch):
    first, _ = picture(tmp_path, "first.png")
    second, _ = picture(tmp_path, "second.png")
    event, api = fake_event()
    successful_materials(api)
    async def put(_url, image):
        if put.calls:
            raise OSError("upload failed")
        put.calls += 1
        return image.md5
    put.calls = 0
    monkeypatch.setattr(gallery, "_put_image", put)

    with pytest.raises(gallery.QQOfficialGalleryUnavailable) as error:
        asyncio.run(gallery.send_qqofficial_gallery(event, [first, second]))

    assert error.value.may_have_been_sent is False
    api.post_group_message.assert_not_awaited()
    assert any(call.args[0].path == "/v2/media-images/upload-1/delete"
               for call in api._http.request.await_args_list)


def test_upload_failure_can_use_public_original_urls(tmp_path, monkeypatch):
    image, _ = picture(tmp_path, "first.png")
    event, api = fake_event()
    api._http.request.side_effect = OSError("unsupported material API")
    monkeypatch.setattr(gallery, "validate_remote_url", AsyncMock())

    receipt = asyncio.run(gallery.send_qqofficial_gallery(
        event, [image], ["https://tiebapic.baidu.com/original.png"],
    ))

    assert receipt.image_count == 1
    api.post_group_message.assert_awaited_once()
    assert "https://tiebapic.baidu.com/original.png" in api.post_group_message.await_args.kwargs["markdown"]["content"]


def test_message_timeout_is_uncertain_and_never_retries(tmp_path, monkeypatch):
    image, _ = picture(tmp_path, "first.png")
    event, api = fake_event()
    successful_materials(api)
    fake_put(monkeypatch)
    api.post_group_message.side_effect = asyncio.TimeoutError

    with pytest.raises(gallery.QQOfficialGalleryError) as error:
        asyncio.run(gallery.send_qqofficial_gallery(event, [image]))

    assert error.value.may_have_been_sent is True
    api.post_group_message.assert_awaited_once()
    assert api._http.request.await_args.args[0].method == "DELETE"


def test_cleanup_failure_does_not_repeat_successful_gallery(tmp_path, monkeypatch):
    image, _ = picture(tmp_path, "first.png")
    event, api = fake_event()
    successful_materials(api)
    original = api._http.request.side_effect
    async def respond(route, **kwargs):
        if route.method == "DELETE":
            raise OSError("cleanup failed")
        return await original(route, **kwargs)
    api._http.request.side_effect = respond
    fake_put(monkeypatch)

    receipt = asyncio.run(gallery.send_qqofficial_gallery(event, [image]))

    assert receipt.message_id == "group-message"
    api.post_group_message.assert_awaited_once()


def test_cancellation_during_completion_observes_and_deletes_owned_material(tmp_path, monkeypatch):
    image, _ = picture(tmp_path, "first.png")
    async def run():
        event, api = fake_event()
        successful_materials(api)
        original = api._http.request.side_effect
        started, finish = asyncio.Event(), asyncio.Event()
        async def respond(route, **kwargs):
            if route.path.endswith("/complete"):
                started.set()
                await finish.wait()
            return await original(route, **kwargs)
        api._http.request.side_effect = respond
        fake_put(monkeypatch)
        task = asyncio.create_task(gallery.send_qqofficial_gallery(event, [image]))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        api.post_group_message.assert_not_awaited()
        assert api._http.request.await_args.args[0].path == "/v2/media-images/upload-1/delete"
    asyncio.run(run())


def test_cancellation_during_cleanup_propagates_after_cleanup_without_resending(tmp_path, monkeypatch):
    image, _ = picture(tmp_path, "first.png")
    async def run():
        event, api = fake_event()
        successful_materials(api)
        original = api._http.request.side_effect
        started, finish = asyncio.Event(), asyncio.Event()
        async def respond(route, **kwargs):
            if route.method == "DELETE":
                started.set()
                await finish.wait()
            return await original(route, **kwargs)
        api._http.request.side_effect = respond
        fake_put(monkeypatch)
        task = asyncio.create_task(gallery.send_qqofficial_gallery(event, [image]))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        api.post_group_message.assert_awaited_once()
        assert api._http.request.await_args.args[0].method == "DELETE"
    asyncio.run(run())


def test_put_binary_upload_has_no_platform_credentials_and_does_not_redirect(tmp_path, monkeypatch):
    _, path = picture(tmp_path, "first.png")
    image = gallery._read_local_image(str(path))
    calls = []
    class Response:
        status = 200
        headers = {"ETag": '"' + image.md5 + '"'}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        def put(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    monkeypatch.setattr(gallery, "validate_remote_url", AsyncMock())
    monkeypatch.setattr(gallery, "create_public_only_connector", lambda: object())
    monkeypatch.setattr(gallery.aiohttp, "ClientSession", lambda **_: Session())

    etag = asyncio.run(gallery._put_image("https://bucket.cos.ap-guangzhou.myqcloud.com/a", image))

    assert etag == image.md5
    assert calls[0][1]["headers"] == {"Content-Type": "image/png"}
    assert calls[0][1]["data"] == image.data
    assert calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("url", ["http://bucket.cos.ap-guangzhou.myqcloud.com/a",
                                "https://127.0.0.1/a", "https://cos.attacker.example/a",
                                "https://name:secret@bucket.cos.ap-guangzhou.myqcloud.com/a"])
def test_upload_rejects_untrusted_presigned_hosts(url):
    with pytest.raises(gallery.QQOfficialGalleryUnavailable):
        gallery._validate_presigned_url(url)


def test_unsupported_platform_or_channel_does_not_upload(tmp_path):
    image, _ = picture(tmp_path, "first.png")
    event, api = fake_event()
    event.get_platform_name = lambda: "aiocqhttp"
    with pytest.raises(gallery.QQOfficialGalleryUnavailable):
        asyncio.run(gallery.send_qqofficial_gallery(event, [image]))
    api._http.request.assert_not_awaited()
    event.get_platform_name = lambda: "qq_official"
    event.message_obj.raw_message = SimpleNamespace(channel_id="channel")
    with pytest.raises(gallery.QQOfficialGalleryUnavailable):
        asyncio.run(gallery.send_qqofficial_gallery(event, [image]))
    api._http.request.assert_not_awaited()


def test_material_image_limits_do_not_attempt_upload(tmp_path, monkeypatch):
    image, _ = picture(tmp_path, "first.png")
    event, api = fake_event()
    monkeypatch.setattr(gallery, "MAX_IMAGE_BYTES", 16)
    with pytest.raises(gallery.QQOfficialGalleryUnavailable):
        asyncio.run(gallery.send_qqofficial_gallery(event, [image]))
    api._http.request.assert_not_awaited()
    api.post_group_message.assert_not_awaited()
