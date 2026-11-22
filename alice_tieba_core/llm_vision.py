"""Bounded Tieba image inputs for multimodal agent tool results.

Only parser-provided image groups are read. The caller owns tool-result
construction; this module never calls a provider or sends a chat message.
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Any, Iterable
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageOps, UnidentifiedImageError

from .downloader.security import (
    UnsafeMediaURLError,
    create_public_only_connector,
    safe_request,
)
from .llm_content import _safe_url
from .parser.platform.tieba import TIEBA_IMAGE_HOSTS


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
MAX_IMAGE_EDGE = 1600
MAX_OUTPUT_IMAGE_BYTES = 1024 * 1024
MAX_SOURCE_GROUPS = 512
MAX_IMAGES = 24
MAX_CANDIDATES = 4
_IMAGE_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://tieba.baidu.com/"}


@dataclass(frozen=True)
class VisionImage:
    data: str
    mime_type: str
    source_url: str
    label: str
    source_ref: dict[str, Any]
    source_refs: tuple[dict[str, Any], ...]
    width: int
    height: int
    animated_first_frame: bool = False
    resized: bool = False


@dataclass
class _Request:
    candidates: list[str]
    source_refs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        ref = self.source_refs[0]
        position = "主楼" if ref["scope"] == "main" else (
            f"第 {ref['floor']} 楼" if ref.get("floor") else "回复楼层"
        )
        return f"帖子 {ref['result_index'] + 1} · {position} · 图片 {ref['image_index'] + 1}"


class _VisionFailure(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class _Budget:
    limit: int
    used: int = 0
    exhausted: bool = False

    def consume(self, amount: int) -> None:
        if self.exhausted or self.used + amount > self.limit:
            self.used = min(self.limit, self.used + amount)
            self.exhausted = True
            raise _VisionFailure("byte_budget")
        self.used += amount


def _initial_image_url(value: Any) -> str:
    url = _safe_url(value)
    if not url:
        return ""
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower().rstrip(".") not in TIEBA_IMAGE_HOSTS:
        return ""
    if parsed.port not in {None, 443 if parsed.scheme == "https" else 80}:
        return ""
    return url


def _group_requests(groups: Any, source_ref: dict[str, Any]) -> list[_Request]:
    if not isinstance(groups, list):
        return []
    requests = []
    for index, group in enumerate(groups[:100]):
        if not isinstance(group, dict) or not isinstance(group.get("candidates"), list):
            continue
        candidates = list(dict.fromkeys(
            url for candidate in group["candidates"][:20]
            if isinstance(candidate, dict) and (url := _initial_image_url(candidate.get("url")))
        ))[:MAX_CANDIDATES]
        if candidates:
            requests.append(_Request(candidates, [{**source_ref, "image_index": index}]))
    return requests


def _interleave(queues: list[list[_Request]]) -> list[_Request]:
    return [item for row in zip_longest(*queues) for item in row if item is not None]


def _collect_requests(payload: Any) -> tuple[list[_Request], int]:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return [], 0
    post_queues = []
    for result_index, result in enumerate(results[:10]):
        if not isinstance(result, dict) or result.get("status") == "error":
            continue
        post_url = _safe_url(result.get("source_url") or result.get("url"))
        ref = {"result_index": result_index, "source_url": post_url, "scope": "main", "floor": 1}
        queues = [_group_requests(result.get("images"), ref)]
        comments = result.get("hot_comments")
        for comment_index, comment in enumerate(comments[:50] if isinstance(comments, list) else []):
            if not isinstance(comment, dict):
                continue
            floor = comment.get("floor")
            comment_id = comment.get("comment_id")
            comment_ref = {
                "result_index": result_index, "source_url": post_url,
                "scope": "comment", "comment_index": comment_index,
            }
            if type(floor) is int and floor > 1:
                comment_ref["floor"] = floor
            if isinstance(comment_id, str) and comment_id.isdecimal() and len(comment_id) <= 30:
                comment_ref["comment_id"] = comment_id
            queues.append(_group_requests(comment.get("images"), comment_ref))
        post_queues.append(_interleave(queues))
    ordered = _interleave(post_queues)
    omitted = max(0, len(ordered) - MAX_SOURCE_GROUPS)
    requests: list[_Request] = []
    by_url: dict[str, _Request] = {}
    for item in ordered[:MAX_SOURCE_GROUPS]:
        previous = next((by_url[url] for url in item.candidates if url in by_url), None)
        if previous is not None:
            previous.source_refs.extend(item.source_refs)
            previous.candidates = list(dict.fromkeys([*previous.candidates, *item.candidates]))[:MAX_CANDIDATES]
            item = previous
        else:
            requests.append(item)
        for url in item.candidates:
            by_url[url] = item
    return requests, omitted


def _prepare_image(data: bytes) -> tuple[bytes, str, int, int, bool, bool]:
    try:
        with Image.open(io.BytesIO(data)) as original:
            width, height = original.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise _VisionFailure("pixels_exceeded")
            first_frame = bool(getattr(original, "is_animated", False))
            original.seek(0)
            # JPEG draft decoding reduces memory before decoding large originals.
            if original.format == "JPEG":
                original.draft("RGB", (MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            prepared = ImageOps.exif_transpose(original)
            has_alpha = "A" in prepared.getbands() or "transparency" in prepared.info
            image = prepared.convert("RGBA" if has_alpha else "RGB")
            prepared.close()
        try:
            before = image.size
            image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
            resized = image.size != before or before != (width, height)
            for _ in range(9):
                buffer = io.BytesIO()
                if has_alpha:
                    image.save(buffer, "PNG", compress_level=4)
                    mime_type = "image/png"
                else:
                    image.save(buffer, "JPEG", quality=86)
                    mime_type = "image/jpeg"
                output = buffer.getvalue()
                if len(output) <= MAX_OUTPUT_IMAGE_BYTES:
                    return output, mime_type, image.width, image.height, first_frame, resized
                smaller = (max(1, round(image.width * 0.8)), max(1, round(image.height * 0.8)))
                image.thumbnail(smaller, Image.Resampling.LANCZOS)
                resized = True
            raise _VisionFailure("output_budget")
        finally:
            image.close()
    except _VisionFailure:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise _VisionFailure("invalid_image") from exc


async def _decode_image(data: bytes, gate: asyncio.Semaphore) -> tuple[bytes, str, int, int, bool, bool]:
    async with gate:
        task = asyncio.create_task(asyncio.to_thread(_prepare_image, data))
        cancellation = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception:
                break
        try:
            result = task.result()
        except Exception as exc:
            if cancellation is not None:
                raise cancellation from exc
            raise
        if cancellation is not None:
            raise cancellation
        return result


async def _download_candidate(
    session: aiohttp.ClientSession,
    url: str,
    *,
    proxy_url: str | None,
    max_image_bytes: int,
    budget: _Budget,
    timeout_seconds: float,
) -> bytes:
    if budget.exhausted:
        raise _VisionFailure("byte_budget")
    response = await safe_request(
        session, "GET", url, proxy=proxy_url, headers=dict(_IMAGE_HEADERS),
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
    )
    try:
        if response.status != 200:
            raise _VisionFailure("download_failed")
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].lower().strip()
        if content_type and not content_type.startswith("image/") and content_type != "application/octet-stream":
            raise _VisionFailure("invalid_image")
        content_length = str(response.headers.get("Content-Length", ""))
        if content_length.isdecimal() and int(content_length) > max_image_bytes:
            raise _VisionFailure("too_large")
        chunks = bytearray()
        async for chunk in response.content.iter_chunked(min(64 * 1024, max_image_bytes + 1)):
            budget.consume(len(chunk))
            if len(chunks) + len(chunk) > max_image_bytes:
                raise _VisionFailure("too_large")
            chunks.extend(chunk)
        if not chunks:
            raise _VisionFailure("invalid_image")
        return bytes(chunks)
    finally:
        response.close()


async def prepare_vision_images(
    payload: dict,
    *,
    max_images: int = 12,
    max_image_bytes: int = MAX_IMAGE_BYTES,
    timeout_seconds: float = 45,
    proxy_url: str | None = None,
    trusted_proxy_urls: Iterable[str] = (),
) -> tuple[list[VisionImage], dict[str, Any]]:
    """Return safe image content plus a provenance/omission report.

    ``payload`` must be the sanitized fetch result. Main images and each
    reply's images are interleaved across posts before the image limit is
    applied, so one long gallery does not hide all reply/post images.
    """
    if type(max_images) is not int or max_images <= 0:
        return [], {"status": "disabled", "requested": 0, "prepared": 0, "skipped": []}
    max_images = min(max_images, MAX_IMAGES)
    max_image_bytes = min(MAX_IMAGE_BYTES, max(1, int(max_image_bytes)))
    try:
        timeout_seconds = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError):
        timeout_seconds = 45
    if not math.isfinite(timeout_seconds):
        timeout_seconds = 45
    elif timeout_seconds <= 0:
        return [], {"status": "deadline_exceeded", "requested": 0, "prepared": 0, "skipped": []}
    timeout_seconds = min(timeout_seconds, 120)
    requests, omitted = _collect_requests(payload)
    report: dict[str, Any] = {
        "status": "none", "requested": len(requests), "prepared": 0,
        "omitted_sources": omitted, "downloaded_bytes": 0, "images": [], "skipped": [],
    }
    if not requests:
        return [], report
    selected = requests[:max_images]
    budget = _Budget(MAX_TOTAL_BYTES)
    output_budget = _Budget(MAX_TOTAL_BYTES)
    workers = asyncio.Semaphore(2)
    decode_gate = asyncio.Semaphore(1)
    outcomes: dict[int, VisionImage | str] = {}
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    connector = create_public_only_connector(trusted_proxy_urls=trusted_proxy_urls)
    try:
        session = aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
        )
    except BaseException:
        await connector.close()
        raise
    async with session:
        async def prepare_one(index: int, request: _Request) -> None:
            reason = "download_failed"
            async with workers:
                for candidate in request.candidates:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        reason = "timeout"
                        break
                    try:
                        raw = await _download_candidate(
                            session, candidate, proxy_url=proxy_url, max_image_bytes=max_image_bytes,
                            budget=budget, timeout_seconds=min(remaining, 15),
                        )
                        output, mime, width, height, animated, resized = await _decode_image(raw, decode_gate)
                        output_budget.consume(len(output))
                        outcomes[index] = VisionImage(
                            data=base64.b64encode(output).decode("ascii"), mime_type=mime,
                            source_url=candidate, label=request.label,
                            source_ref=dict(request.source_refs[0]),
                            source_refs=tuple(dict(ref) for ref in request.source_refs),
                            width=width, height=height, animated_first_frame=animated, resized=resized,
                        )
                        return
                    except UnsafeMediaURLError:
                        reason = "unsafe_url"
                    except _VisionFailure as exc:
                        reason = exc.reason
                        if reason in {"byte_budget", "output_budget"}:
                            break
                    except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
                        reason = "timeout" if asyncio.get_running_loop().time() >= deadline else "download_failed"
                    except Exception:
                        reason = "invalid_image"
            outcomes[index] = reason

        tasks = [asyncio.create_task(prepare_one(index, item)) for index, item in enumerate(selected)]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout_seconds)
        except asyncio.TimeoutError:
            for index in range(len(selected)):
                outcomes.setdefault(index, "timeout")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    images = []
    for index, request in enumerate(requests):
        outcome = outcomes.get(index, "image_budget")
        provenance = {"label": request.label, "source_ref": dict(request.source_refs[0]),
                      "source_refs": [dict(ref) for ref in request.source_refs]}
        if isinstance(outcome, VisionImage):
            images.append(outcome)
            report["images"].append({**provenance, "source_url": outcome.source_url,
                                     "mime_type": outcome.mime_type, "width": outcome.width,
                                     "height": outcome.height, "animated_first_frame": outcome.animated_first_frame,
                                     "resized": outcome.resized})
        else:
            report["skipped"].append({**provenance, "reason": outcome})
    report["prepared"] = len(images)
    report["downloaded_bytes"] = budget.used
    report["status"] = "ok" if len(images) == len(requests) and not omitted else "partial" if images else "unavailable"
    return images, report
