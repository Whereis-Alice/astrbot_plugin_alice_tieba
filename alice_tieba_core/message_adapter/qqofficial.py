"""QQ Official native Markdown galleries with independently clickable images.

Group and C2C Markdown is available to all QQ bots since 2026-04-23.
Local original images use QQ's material API, so this path needs neither an
external image host nor a public AstrBot callback address.
"""

import asyncio
import hashlib
import io
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import aiohttp
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from PIL import Image as PILImage

from ..downloader.security import create_public_only_connector, validate_remote_url
from ..logger import logger


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_GALLERY_IMAGES = 24
API_TIMEOUT = 30
_CONTENT_TYPES = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "GIF": ("image/gif", ".gif"),
    "WEBP": ("image/webp", ".webp"),
}


class QQOfficialGalleryError(RuntimeError):
    """Gallery delivery failed; uncertain sends must never be resent."""

    def __init__(self, message: str, *, may_have_been_sent: bool = False):
        super().__init__(message)
        self.may_have_been_sent = may_have_been_sent


class QQOfficialGalleryUnavailable(QQOfficialGalleryError):
    """No gallery message was sent; an ordinary-image fallback is safe."""


@dataclass(frozen=True)
class QQOfficialGalleryReceipt:
    message_id: str
    image_count: int


@dataclass(frozen=True)
class _LocalImage:
    file_name: str
    content_type: str
    data: bytes
    width: int
    height: int
    md5: str
    sha1: str


def _read_local_image(reference: str) -> _LocalImage:
    value = str(reference or "").strip()
    if value.lower().startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            raise QQOfficialGalleryUnavailable("原图不是本地下载文件")
        value = unquote(parsed.path)
        if len(value) >= 3 and value[0] == "/" and value[2] == ":":
            value = value[1:]
    elif value.lower().startswith(("http:", "https:", "base64:", "data:")):
        raise QQOfficialGalleryUnavailable("原图尚未下载为本地文件")
    path = Path(value)
    if not value or not path.is_file():
        raise QQOfficialGalleryUnavailable("本地原图不存在")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise QQOfficialGalleryUnavailable("QQ 原生多图单张图片不能超过 10MB")
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise QQOfficialGalleryUnavailable("QQ 原生多图单张图片不能超过 10MB")
    try:
        with PILImage.open(io.BytesIO(data)) as image:
            content = _CONTENT_TYPES.get(image.format)
            width, height = image.size
            if content is None or width <= 0 or height <= 0:
                raise QQOfficialGalleryUnavailable("QQ 原生多图不支持这个图片格式")
            if width * height > MAX_IMAGE_PIXELS:
                raise QQOfficialGalleryUnavailable("原图像素超出安全读取范围")
            image.verify()
    except (OSError, ValueError, PILImage.DecompressionBombError) as exc:
        raise QQOfficialGalleryUnavailable("原图无法安全读取") from exc
    content_type, suffix = content
    return _LocalImage(
        file_name="tieba-original" + suffix, content_type=content_type, data=data,
        width=width, height=height,
        md5=hashlib.md5(data, usedforsecurity=False).hexdigest(),
        sha1=hashlib.sha1(data, usedforsecurity=False).hexdigest(),
    )


async def _platform_request(api: Any, method: str, path: str, payload: dict | None = None) -> Any:
    request = getattr(getattr(api, "_http", None), "request", None)
    if not callable(request):
        raise QQOfficialGalleryUnavailable("当前 QQ SDK 未提供素材上传接口")
    try:
        from botpy.http import Route
    except ImportError as exc:
        raise QQOfficialGalleryUnavailable("当前 QQ SDK 未提供素材上传接口") from exc
    kwargs = {"json": payload} if payload is not None else {}
    return await asyncio.wait_for(request(Route(method, path), **kwargs), API_TIMEOUT)


def _validate_presigned_url(value: object) -> str:
    url = str(value or "")
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise QQOfficialGalleryUnavailable("QQ 上传地址无效") from exc
    if (
        parsed.scheme != "https" or port not in (None, 443)
        or parsed.username is not None or parsed.password is not None
        or parsed.fragment or any(char in url for char in "\r\n\t")
        or not hostname.endswith(".myqcloud.com")
        or ".cos." not in hostname
    ):
        raise QQOfficialGalleryUnavailable("QQ 上传地址不是受支持的腾讯 COS 地址")
    return url


async def _put_image(url: str, image: _LocalImage) -> str:
    """PUT binary bytes without QQ access tokens, cookies, or other credentials."""
    url = _validate_presigned_url(url)
    await validate_remote_url(url)
    connector = create_public_only_connector()
    try:
        session = aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            trust_env=False,
        )
    except BaseException:
        await connector.close()
        raise
    async with session:
        async with session.put(
            url, data=image.data,
            headers={"Content-Type": image.content_type}, allow_redirects=False,
        ) as response:
            if response.status not in (200, 201, 204):
                raise QQOfficialGalleryUnavailable("QQ 原图上传未完成")
            etag = str(response.headers.get("ETag") or "").strip('"')
            if not re.fullmatch(r"[a-fA-F0-9]{32}", etag) or etag.lower() != image.md5:
                raise QQOfficialGalleryUnavailable("QQ 原图上传校验不一致")
            return etag.lower()


async def _complete_material(api: Any, upload_id: str, md5: str, owned_ids: list[str]) -> dict:
    """Observe completion even on cancellation so its owned material is cleaned."""
    task = asyncio.create_task(_platform_request(
        api, "POST", "/v2/media-images/complete", {"upload_id": upload_id, "md5": md5},
    ))
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            break
    try:
        result = task.result()
    except BaseException as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
    if isinstance(result, dict) and result.get("image_id"):
        identifier = str(result["image_id"])
        if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", identifier):
            owned_ids.append(identifier)
    if cancellation is not None:
        raise cancellation
    if not isinstance(result, dict):
        raise QQOfficialGalleryUnavailable("QQ 未返回原图素材引用")
    return result


async def _register_local_image(api: Any, reference: str, owned_ids: list[str]) -> tuple[str, int, int]:
    image = await asyncio.to_thread(_read_local_image, reference)
    prepared = await _platform_request(api, "POST", "/v2/media-images/prepare", {
        "file_name": image.file_name, "file_size": len(image.data),
        "content_type": image.content_type, "md5": image.md5, "sha1": image.sha1,
    })
    if not isinstance(prepared, dict) or not prepared.get("upload_id"):
        raise QQOfficialGalleryUnavailable("QQ 未返回原图上传凭据")
    etag = await _put_image(prepared.get("presigned_url"), image)
    result = await _complete_material(api, str(prepared["upload_id"]), etag, owned_ids)
    url = str(result.get("url") or "")
    if (
        str(result.get("image_id") or "") not in owned_ids
        or not re.fullmatch(r"qqbot-media://image/v1/[A-Za-z0-9._~-]+", url)
    ):
        raise QQOfficialGalleryUnavailable("QQ 返回的原图素材引用无效")
    return url, image.width, image.height


async def _public_image_reference(url: object) -> tuple[str, int, int]:
    value = str(url or "").strip()
    if not value or any(char in value for char in "\r\n\t"):
        raise QQOfficialGalleryUnavailable("原图没有可公开访问的链接")
    try:
        await validate_remote_url(value)
    except Exception as exc:
        raise QQOfficialGalleryUnavailable("原图链接无法公开访问") from exc
    # Parentheses and whitespace must not terminate the Markdown image URL.
    return quote(value, safe=":/?&=%._~+-#"), 0, 0


def _reply_method(event: AstrMessageEvent) -> tuple[Any, dict[str, Any]]:
    api = getattr(getattr(event, "bot", None), "api", None)
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    group_id = getattr(raw, "group_openid", None)
    if group_id and callable(getattr(api, "post_group_message", None)):
        return api.post_group_message, {"group_openid": str(group_id)}
    author = getattr(raw, "author", None)
    user_id = getattr(author, "user_openid", None)
    if user_id and callable(getattr(api, "post_c2c_message", None)):
        return api.post_c2c_message, {"openid": str(user_id)}
    raise QQOfficialGalleryUnavailable("当前会话不是 QQ 官方群聊或单聊")


async def _delete_materials(api: Any, owned_ids: list[str]) -> None:
    for identifier in owned_ids:
        try:
            await _platform_request(api, "DELETE", f"/v2/media-images/{identifier}/delete")
        except Exception:
            logger.warning("清理本次 QQ 原图素材失败，可在 QQ 素材管理中重试")


async def send_qqofficial_gallery(
    event: AstrMessageEvent,
    images: list[Image],
    source_urls: list[str] | None = None,
) -> QQOfficialGalleryReceipt:
    """Send only the supplied original images as one native Markdown message.

    The card is sent separately by the caller. ``source_urls`` are optional
    per-image public URL fallbacks, not post URLs. A timeout after starting
    message delivery raises with ``may_have_been_sent=True``; do not resend.
    """
    if event.get_platform_name() != "qq_official":
        raise QQOfficialGalleryUnavailable("此多图发送方式仅用于 QQ 官方 Bot")
    if not images or len(images) > MAX_GALLERY_IMAGES:
        raise QQOfficialGalleryUnavailable("本次原图超过插件单条图库的安全数量范围")
    method, destination = _reply_method(event)
    api = event.bot.api
    owned_ids: list[str] = []
    references: list[tuple[str, int, int]] = []
    try:
        for index, component in enumerate(images):
            if not isinstance(component, Image):
                raise QQOfficialGalleryUnavailable("原生多图只接受原图图片组件")
            source = str(component.file or "")
            if source.lower().startswith(("http://", "https://")):
                reference = await _public_image_reference(source)
            else:
                try:
                    reference = await _register_local_image(api, source, owned_ids)
                except Exception as exc:
                    fallback = source_urls[index] if source_urls and index < len(source_urls) else ""
                    if not fallback:
                        raise QQOfficialGalleryUnavailable("原图未能注册到 QQ 素材服务") from exc
                    reference = await _public_image_reference(fallback)
            references.append(reference)

        lines = []
        for index, (url, width, height) in enumerate(references, start=1):
            dimensions = ""
            if width and height:
                display_width = min(width, 800)
                display_height = max(1, round(height * display_width / width))
                dimensions = f" #{display_width}px #{display_height}px"
            lines.append(f"![原图 {index}{dimensions}]({url})")
        markdown = "\n\n".join(lines)
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        payload = {**destination, "msg_type": 2, "markdown": {"content": markdown},
                   "msg_seq": secrets.randbelow(2_000_000_000) + 10001}
        if message_id:
            payload["msg_id"] = message_id
        try:
            result = await asyncio.wait_for(method(**payload), API_TIMEOUT)
        except (asyncio.TimeoutError, OSError, aiohttp.ClientError) as exc:
            raise QQOfficialGalleryError(
                "QQ 原生多图发送结果未确认，请勿重复解析", may_have_been_sent=True,
            ) from exc
        except Exception as exc:
            raise QQOfficialGalleryError("QQ 拒绝了原生多图消息") from exc
        response_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
        if not response_id:
            raise QQOfficialGalleryError(
                "QQ 未返回原生多图发送回执，请勿重复解析", may_have_been_sent=True,
            )
        if isinstance(event, AstrMessageEvent):
            try:
                await AstrMessageEvent.send(event, event.chain_result([Plain(markdown)]))
            except Exception:
                logger.debug("原生多图已发送，事件发送统计记录失败")
        return QQOfficialGalleryReceipt(str(response_id), len(references))
    finally:
        cleanup = asyncio.create_task(_delete_materials(api, owned_ids))
        cleanup_cancellation = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                cleanup_cancellation = cleanup_cancellation or exc
                continue
        cleanup.result()
        if cleanup_cancellation is not None:
            raise cleanup_cancellation
