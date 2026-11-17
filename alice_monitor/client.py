"""Bounded client calls for public user threads and original-poster floors.

The legacy form signature matches the existing Tieba parser. Only fixed Baidu
HTTPS endpoints are contacted; redirects and non-JSON challenge pages fail closed.
"""

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

try:
    from ..alice_tieba_core.downloader.security import create_public_only_connector
    from ..alice_tieba_core.parser.platform.tieba import TiebaParser
except ImportError:
    from alice_tieba_core.downloader.security import create_public_only_connector
    from alice_tieba_core.parser.platform.tieba import TiebaParser


@dataclass
class Page:
    items: list[dict[str, Any]]
    has_more: bool
    total_pages: int = 1
    owner_id: str = ""
    title: str = ""
    name: str = ""


def number(value: Any, default: int = 0) -> int:
    try:
        return int(value) if not isinstance(value, bool) else default
    except (TypeError, ValueError, OverflowError):
        return default


def clean_text(value: Any, limit: int = 2000) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()[:limit]


class TiebaMonitorClient:
    def __init__(self, *, cookie: str = "", proxy: str | None = None,
                 interval: float = 2.0) -> None:
        self.cookie = cookie if not re.search(r"[\r\n]", cookie) else ""
        self.proxy = proxy
        self.interval = max(1.0, float(interval))
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._next_request = 0.0

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _request(self, path: str, **params: str) -> dict[str, Any]:
        async with self._lock:
            wait = self._next_request - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_request = time.monotonic() + self.interval
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    connector=create_public_only_connector(
                        trusted_proxy_urls=[self.proxy] if self.proxy else []),
                    timeout=aiohttp.ClientTimeout(total=25),
                )
            form = {"_client_type": "2", "_client_version": "12.64.1.1", **params}
            joined = "".join(f"{key}={value}" for key, value in sorted(form.items()))
            form["sign"] = hashlib.md5(
                (joined + "tiebaclient!!!").encode(), usedforsecurity=False
            ).hexdigest().upper()
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://tieba.baidu.com/"}
            if self.cookie:
                headers["Cookie"] = self.cookie
            async with self._session.post(
                "https://tiebac.baidu.com" + path, data=form,
                headers=headers, proxy=self.proxy, allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ValueError(f"贴吧请求失败（HTTP {response.status}），请检查访问限制后重试")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        raise ValueError("贴吧监控响应超过 4 MiB 限制")
                    chunks.append(chunk)
                try:
                    data = json.loads(b"".join(chunks))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ValueError("贴吧返回安全验证或无效 JSON，请检查 Cookie/代理后重试") from exc
                if not isinstance(data, dict) or str(data.get("error_code")) != "0":
                    raise ValueError(TiebaParser._api_error(data if isinstance(data, dict) else {}))
                return data

    async def thread_page(self, target: str, page: int) -> Page:
        data = await self._request("/c/f/pb/page", kz=target, pn=str(page),
                                   rn="30", lz="1", with_floor="0")
        thread = data.get("thread")
        if not isinstance(thread, dict) or str(thread.get("id", "")) != target:
            raise ValueError("贴吧未返回请求的帖子，无法确认追踪水位")
        author = thread.get("author") or {}
        owner = str(author.get("id") or "")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", owner):
            raise ValueError("贴吧未返回楼主 UID，无法安全追踪")
        posts = data.get("post_list")
        if not isinstance(posts, list):
            raise ValueError("贴吧未返回楼层列表，无法确认追踪水位")
        users = {str(u.get("id")): u for u in data.get("user_list", []) if isinstance(u, dict)}
        rows = []
        for post in posts:
            if not isinstance(post, dict) or TiebaParser._is_ad(post):
                continue
            user = TiebaParser._user(post, users)
            if str(post.get("author_id") or user.get("id") or "") != owner:
                continue
            floor = number(post.get("floor"))
            identifier = str(post.get("id") or "")
            if floor < 1 or not identifier or post.get("is_deleted") in (True, 1, "1"):
                continue
            body, images, videos, _ = TiebaParser._content(post.get("content"))
            suffix = ""
            if images:
                suffix += f"\n[图片 {len(images)} 张]"
            if videos:
                suffix += "\n[视频]"
            rows.append({"key": identifier, "floor": floor,
                         "time": number(post.get("time")),
                         "title": clean_text(thread.get("title"), 200),
                         "text": clean_text(body + suffix),
                         "url": f"https://tieba.baidu.com/p/{target}?pid={identifier}#{identifier}"})
        page_info = data.get("page")
        if not isinstance(page_info, dict) or number(page_info.get("total_page")) < 1:
            raise ValueError("贴吧未返回有效楼层分页，无法确认追踪水位")
        total = number(page_info["total_page"])
        if page > total:
            raise ValueError("贴吧分页发生变化，请重新检查")
        if page == 1 and not any(item["floor"] == 1 for item in rows):
            raise ValueError("贴吧未返回楼主主楼，帖子可能已删除或访问受限")
        return Page(sorted(rows, key=lambda row: row["floor"]), page < total, total,
                    owner, clean_text(thread.get("title"), 200),
                    clean_text(author.get("name_show") or author.get("name"), 100))

    async def user_page(self, target: str, page: int) -> Page:
        data = await self._request("/c/u/feed/userpost", uid=target, is_thread="1",
                                   need_content="1", pn=str(page), rn="20")
        if str(data.get("hide_post", "0")) != "0":
            raise ValueError("该用户的主题帖受到隐私限制，未更新追踪水位")
        posts = data.get("post_list")
        if not isinstance(posts, list):
            raise ValueError("贴吧未返回用户主题帖列表，用户可能设置了隐私限制")
        rows = []
        for post in posts:
            if not isinstance(post, dict) or TiebaParser._is_ad(post):
                continue
            if str(post.get("is_thread", "1")) != "1":
                continue
            if str(post.get("user_id", target)) != target:
                raise ValueError("用户主题帖作者与请求 UID 不一致")
            if post.get("is_post_deleted") in (True, 1, "1"):
                continue
            identifier = str(post.get("thread_id") or post.get("tid") or "")
            if not re.fullmatch(r"[1-9][0-9]{0,19}", identifier):
                raise ValueError("用户主题帖列表缺少帖子 ID，未更新追踪水位")
            created = number(post.get("create_time") or post.get("time"))
            if created <= 0:
                raise ValueError("用户主题帖缺少发布时间，未更新追踪水位")
            body, images, videos, _ = TiebaParser._content(
                post.get("first_post_content") or post.get("rich_abstract") or post.get("abstract"))
            if images:
                body += f"\n[图片 {len(images)} 张]"
            if videos:
                body += "\n[视频]"
            rows.append({"key": identifier, "time": created,
                         "title": clean_text(post.get("title"), 200),
                         "text": clean_text(body), "url": f"https://tieba.baidu.com/p/{identifier}"})
        page_info = data.get("page") or {}
        # The live legacy endpoint returns 59 rows for rn=20, and no page
        # metadata. An empty/repeated page is the only reliable end marker.
        has_more = str(data.get("has_more", page_info.get("has_more", int(bool(posts))))) == "1"
        user = data.get("user") or data.get("view_user") or {}
        return Page(rows, has_more, max(page + int(has_more), 1),
                    target, name=clean_text(user.get("name_show") or user.get("name")
                                           or (posts[0].get("name_show") if posts else ""), 100))

    async def resolve_user(self, target: str) -> tuple[str, str]:
        if not re.fullmatch(r"[1-9][0-9]{0,19}", target):
            raise ValueError("请使用数字贴吧 UID；有帖子链接时可直接关注作者")
        data = await self._request("/c/u/user/profile", uid=target, pn="1", need_post_count="1")
        user = data.get("user")
        if not isinstance(user, dict):
            raise ValueError("贴吧未返回用户资料，无法确认 UID")
        uid = str(user.get("id") or "")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", uid) or (target.isdecimal() and uid != target):
            raise ValueError("贴吧用户资料与请求 UID 不一致")
        return uid, clean_text(user.get("name_show") or user.get("name"), 100)
