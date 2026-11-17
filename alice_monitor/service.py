"""Durable subscriptions, staged pagination and an at-least-once outbox."""

import asyncio
import copy
import hashlib
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from .client import Page, TiebaMonitorClient, clean_text


STATE_KEY = "alice_tieba_monitor_v1"
MAX_PENDING = 1000
MAX_USER_SEEN = 10000


def stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MonitorService:
    """Only this service owns cursors; web and chat share the same API.

    A notification is persisted before sending and removed only after an
    affirmative adapter result. A process crash after the platform accepts a
    message but before KV acknowledgement can repeat that message on restart.
    """

    def __init__(self, plugin: Any, config: Any, *, client: Any = None,
                 send: Callable[[str, str], Awaitable[Any]] | None = None) -> None:
        self.plugin = plugin
        self.config = config
        cfg = getattr(plugin, "config_manager", None)
        cookie = getattr(getattr(cfg, "tieba", None), "cookie", "")
        proxy_cfg = getattr(cfg, "proxy", None)
        proxy = getattr(proxy_cfg, "address", None) if getattr(proxy_cfg, "tieba_use_proxy", False) else None
        self.client = client or TiebaMonitorClient(cookie=cookie, proxy=proxy,
                                                  interval=self._setting("request_interval_seconds", 2, 1, 60))
        self._send = send or self._send_message
        self._state: dict[str, Any] = {"version": 1, "subscriptions": {}, "events": []}
        self._lock = asyncio.Lock()
        self._check_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._manual_task: asyncio.Task | None = None
        self._loaded = False
        self._closed = False
        self._last_check = ""

    def _settings(self) -> dict:
        raw = self.config.get("monitoring", self.config)
        return raw if isinstance(raw, dict) else {}

    def _setting(self, key: str, default: float, lower: float, upper: float) -> float:
        try:
            value = float(self._settings().get(key, default))
            return min(upper, max(lower, value))
        except (ValueError, TypeError, OverflowError):
            return default

    @property
    def enabled(self) -> bool:
        return self._settings().get("enabled", False) is True

    async def _send_message(self, session: str, text: str) -> Any:
        from astrbot.api.event import MessageChain
        return await self.plugin.context.send_message(session, MessageChain().message(text))

    async def initialize(self) -> None:
        async with self._lock:
            if self._loaded:
                return
            state = await self.plugin.get_kv_data(STATE_KEY, None)
            if state is not None:
                if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("subscriptions"), dict):
                    raise ValueError("贴吧订阅存储格式无效；请保留原数据后检查")
                self._state = copy.deepcopy(state)
                self._state.setdefault("events", [])
            self._loaded = True

    async def _save(self) -> None:
        await self.plugin.put_kv_data(STATE_KEY, copy.deepcopy(self._state))

    def _event(self, subscription_id: str, level: str, message: str) -> None:
        self._state["events"].append({"id": uuid.uuid4().hex[:12], "time": stamp(),
                                      "subscription_id": subscription_id,
                                      "level": level, "message": clean_text(message, 300)})
        self._state["events"] = self._state["events"][-200:]

    async def start(self) -> None:
        await self.initialize()
        if self._task and not self._task.done():
            return
        self._closed = False
        self._task = asyncio.create_task(self._loop(), name="alice-tieba-monitor")

    async def close(self) -> None:
        self._closed = True
        tasks = [task for task in (self._task, self._manual_task)
                 if task and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = self._manual_task = None
        await self.client.close()

    async def _loop(self) -> None:
        while not self._closed:
            if self.enabled:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger = getattr(self.plugin, "logger", None)
                    if logger:
                        logger.warning(f"爱丽丝贴吧监控检查失败: {type(exc).__name__}")
            await asyncio.sleep(self._setting("poll_interval_seconds", 300, 60, 86400))

    @staticmethod
    def _public(subscription: dict) -> dict:
        keys = ("id", "kind", "target", "session", "label", "keywords", "enabled", "baseline_ready",
                "last_check", "last_success", "last_error", "failures", "created_at", "name", "title")
        result = {key: copy.deepcopy(subscription.get(key)) for key in keys}
        result["pending_count"] = len(subscription.get("pending", []))
        result["scan_page"] = subscription.get("scan_page", 1)
        return result

    async def list_subscriptions(self) -> list[dict]:
        await self.initialize()
        async with self._lock:
            return [self._public(sub) for sub in self._state["subscriptions"].values()]

    async def status(self) -> dict:
        subs = await self.list_subscriptions()
        return {"enabled": self.enabled, "running": bool(self._task and not self._task.done()),
                "subscription_count": len(subs), "active_count": sum(bool(s["enabled"]) for s in subs),
                "pending_count": sum(s["pending_count"] for s in subs), "last_check": self._last_check}

    async def events(self, limit: int = 100) -> list[dict]:
        await self.initialize()
        async with self._lock:
            return copy.deepcopy(self._state["events"][-max(1, min(200, int(limit))):][::-1])

    @staticmethod
    def _keywords(keywords: Any) -> list[str]:
        if keywords is None:
            return []
        if not isinstance(keywords, list) or len(keywords) > 20 or any(not isinstance(k, str) for k in keywords):
            raise ValueError("关键词应为至多 20 个字符串组成的列表")
        if any(len(k.strip()) > 100 for k in keywords):
            raise ValueError("每个关键词不能超过 100 个字符")
        return list(dict.fromkeys(k.strip() for k in keywords if k.strip()))

    async def add_subscription(self, kind: str, target: str, session: str,
                               label: str = "", keywords: Any = None, enabled: bool = True) -> dict:
        await self.initialize()
        if kind not in {"user", "thread"}:
            raise ValueError("订阅类型只能为 user 或 thread")
        target = str(target).strip()
        inferred_name = ""
        user_verified = False
        if target.startswith(("https://", "http://")):
            try:
                from ..alice_tieba_core.parser.platform.tieba import TiebaParser
            except ImportError:
                from alice_tieba_core.parser.platform.tieba import TiebaParser
            thread_id = TiebaParser._thread_id(target) or ""
            if not thread_id:
                raise ValueError("不是支持的贴吧帖子链接")
            if kind == "user":
                author_page = await self.client.thread_page(thread_id, 1)
                target, inferred_name = author_page.owner_id, author_page.name
                user_verified = True
            else:
                target = thread_id
        if not re.fullmatch(r"[1-9][0-9]{0,19}", target):
            raise ValueError("关注用户请填写数字贴吧 UID 或该用户的帖子链接；追踪请填写帖子 ID/链接")
        if not isinstance(session, str) or not re.fullmatch(r"[^\s:]{1,100}:[^\s:]{1,100}:[^\s]{1,300}", session):
            raise ValueError("目标会话应为 AstrBot unified_msg_origin，可用“爱丽丝贴吧 会话”获取")
        if not isinstance(enabled, bool):
            raise ValueError("enabled 应为布尔值")
        keywords = self._keywords(keywords)
        async with self._lock:
            subs = self._state["subscriptions"]
            if len(subs) >= self._setting("max_subscriptions", 50, 1, 200):
                raise ValueError("订阅数量已达到配置上限")
            if any(s["kind"] == kind and s["target"] == target and s["session"] == session for s in subs.values()):
                raise ValueError("该目标已订阅到此会话，请编辑现有订阅")
            identifier = uuid.uuid4().hex[:12]
            sub = {"id": identifier, "kind": kind, "target": target, "session": session,
                   "label": clean_text(label, 100), "keywords": keywords, "enabled": enabled,
                   "baseline_ready": False, "last_check": "", "last_success": "", "last_error": "",
                   "failures": 0, "created_at": stamp(), "name": inferred_name, "title": "",
                   "watermark_time": 0, "watermark_floor": 0, "baseline_time": None,
                   "seen": [], "pending": [],
                   "scan_page": 1, "scan_fingerprints": [], "next_check_at": 0,
                   "scan_cutoff_time": None, "scan_cutoff_floor": None,
                   "user_verified": user_verified}
            subs[identifier] = sub
            self._event(identifier, "info", "已添加订阅；首次成功检查只建立基线，不发送历史内容")
            await self._save()
            return self._public(sub)

    async def update_subscription(self, identifier: str, patch: dict) -> dict:
        await self.initialize()
        if not isinstance(patch, dict) or set(patch) - {"label", "keywords", "enabled", "session"}:
            raise ValueError("只能编辑备注、关键词、启停状态和目标会话；更换目标请重新订阅")
        async with self._lock:
            sub = self._state["subscriptions"].get(identifier)
            if sub is None:
                raise ValueError("订阅不存在")
            if "enabled" in patch and not isinstance(patch["enabled"], bool):
                raise ValueError("enabled 应为布尔值")
            if "session" in patch:
                session = patch["session"]
                if not isinstance(session, str) or not re.fullmatch(r"[^\s:]{1,100}:[^\s:]{1,100}:[^\s]{1,300}", session):
                    raise ValueError("目标会话格式无效")
                if any(s["id"] != identifier and s["kind"] == sub["kind"] and s["target"] == sub["target"] and s["session"] == session for s in self._state["subscriptions"].values()):
                    raise ValueError("相同目标已订阅到此会话")
                sub["session"] = session
            if "keywords" in patch:
                sub["keywords"] = self._keywords(patch["keywords"])
            if "label" in patch:
                sub["label"] = clean_text(patch["label"], 100)
            if "enabled" in patch:
                sub["enabled"] = patch["enabled"]
                sub["next_check_at"] = 0
            self._event(identifier, "info", "订阅配置已更新")
            await self._save()
            return self._public(sub)

    async def remove_subscription(self, identifier: str) -> bool:
        await self.initialize()
        async with self._lock:
            removed = self._state["subscriptions"].pop(identifier, None)
            if removed is None:
                raise ValueError("订阅不存在")
            self._event(identifier, "info", "已取消订阅并清除待发送队列")
            await self._save()
            return True

    async def check_now(self, identifier: str | None = None, *, session: str | None = None) -> dict:
        await self.initialize()
        if identifier and identifier not in self._state["subscriptions"]:
            raise ValueError("订阅不存在")
        if not self.enabled:
            return {"queued": False, "message": "推送总开关已关闭，请先在配置页开启"}
        if self._manual_task and not self._manual_task.done():
            return {"queued": False, "message": "已有手动检查正在进行"}
        self._manual_task = asyncio.create_task(self.run_once(identifier, force=True, session=session), name="alice-tieba-manual-check")
        self._manual_task.add_done_callback(self._observe_manual)
        return {"queued": True, "message": "已安排检查，可在活动记录查看结果"}

    def _observe_manual(self, task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def run_once(self, identifier: str | None = None, *, force: bool = False,
                       session: str | None = None) -> dict:
        """Perform a bounded polling round. Public for deterministic integration tests."""
        await self.initialize()
        if not self.enabled:
            return {"checked": 0, "sent": 0}
        async with self._check_lock:
            async with self._lock:
                identifiers = [identifier] if identifier else list(self._state["subscriptions"])
            checked = sent = 0
            for sub_id in identifiers:
                async with self._lock:
                    stored = self._state["subscriptions"].get(sub_id)
                    if not stored or not stored["enabled"] or (session is not None and stored["session"] != session) or (not force and stored.get("next_check_at", 0) > time.time()):
                        continue
                    sub = copy.deepcopy(stored)
                checked += 1
                sub["last_check"] = stamp()
                try:
                    sent += await self._check_subscription(sub)
                    sub["last_success"] = stamp()
                    sub["last_error"] = ""
                    sub["failures"] = 0
                    sub["next_check_at"] = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if sub["kind"] == "thread" and "分页发生变化" in str(exc):
                        sub["scan_page"] = 1
                        sub["scan_fingerprints"] = []
                        sub["scan_cutoff_time"] = sub["scan_cutoff_floor"] = None
                    sub["failures"] = min(20, sub.get("failures", 0) + 1)
                    sub["last_error"] = clean_text(str(exc), 250) or type(exc).__name__
                    sub["next_check_at"] = time.time() + min(3600, 60 * 2 ** min(sub["failures"], 6))
                    async with self._lock:
                        self._event(sub_id, "error", sub["last_error"])
                finally:
                    await self._commit(sub)
            self._last_check = stamp()
            return {"checked": checked, "sent": sent}

    async def _commit(self, sub: dict) -> bool:
        async with self._lock:
            current = self._state["subscriptions"].get(sub["id"])
            if current is None:
                return False
            # UI edits made while a request was in flight take precedence.
            for key in ("label", "keywords", "enabled", "session"):
                sub[key] = current[key]
            self._state["subscriptions"][sub["id"]] = copy.deepcopy(sub)
            await self._save()
            return True

    async def _can_send(self, sub: dict) -> bool:
        async with self._lock:
            current = self._state["subscriptions"].get(sub["id"])
            if not current or not current["enabled"] or not self.enabled:
                return False
            sub["session"] = current["session"]
            sub["label"] = current["label"]
            sub["keywords"] = current["keywords"]
            return True

    def _matches(self, sub: dict, item: dict) -> bool:
        words = sub.get("keywords") or []
        text = (item.get("title", "") + "\n" + item.get("text", "")).casefold()
        return not words or any(word.casefold() in text for word in words)

    def _ack(self, sub: dict, item: dict) -> None:
        if item["key"] not in sub["seen"]:
            sub["seen"].append(item["key"])
        if sub["kind"] == "thread":
            sub["seen"] = sub["seen"][-2000:]
        sub["watermark_floor"] = max(sub["watermark_floor"], item.get("floor", 0))
        sub["watermark_time"] = max(sub["watermark_time"], item.get("time", 0))

    async def _drain(self, sub: dict, budget: int) -> int:
        sent = 0
        while sub["pending"] and sent < budget:
            if not await self._can_send(sub):
                break
            item = sub["pending"][0]
            if self._matches(sub, item):
                prefix = "关注用户发布新帖" if sub["kind"] == "user" else f"楼主更新 · 第 {item.get('floor', '?')} 楼"
                label = sub["label"] or sub["name"] or sub["target"]
                title = item.get("title") or sub.get("title") or "贴吧帖子"
                limit = int(self._setting("summary_chars", 500, 100, 2000))
                text = f"爱丽丝的贴吧工具 · {prefix}\n{label}\n{title}\n{item.get('text', '')[:limit]}\n{item['url']}"
                result = await self._send(sub["session"], text)
                if result is not True:
                    raise ValueError("推送未获得消息平台确认，已保留待发送内容，下次检查重试")
                sent += 1
                async with self._lock:
                    self._event(sub["id"], "success", f"已推送：{title[:100]}")
            self._ack(sub, item)
            sub["pending"].pop(0)
            await self._commit(sub)
        return sent

    async def _check_subscription(self, sub: dict) -> int:
        budget = int(self._setting("max_push_per_check", 10, 1, 50))
        sent = await self._drain(sub, budget)
        if sub["pending"] or sent >= budget:
            return sent
        if not sub["baseline_ready"]:
            if sub["kind"] == "user" and not sub.get("user_verified") and hasattr(self.client, "resolve_user"):
                uid, name = await self.client.resolve_user(sub["target"])
                if uid != sub["target"]:
                    raise ValueError("贴吧用户资料与订阅 UID 不一致")
                sub["name"] = name
                sub["user_verified"] = True
            first = await (self.client.user_page(sub["target"], 1) if sub["kind"] == "user"
                           else self.client.thread_page(sub["target"], 1))
            sub["name"], sub["title"] = first.name or sub["name"], first.title
            last = first
            if sub["kind"] == "thread" and first.total_pages > 1:
                last = await self.client.thread_page(sub["target"], first.total_pages)
                if last.owner_id != first.owner_id:
                    raise ValueError("楼主信息在建立基线期间变化，未保存水位")
            for item in first.items + (last.items if last is not first else []):
                self._ack(sub, item)
            if sub["kind"] == "user" and not first.items:
                sub["watermark_time"] = int(time.time())
            sub["baseline_time"] = sub["watermark_time"]
            sub["baseline_ready"] = True
            sub["owner_id"] = first.owner_id
            if sub["kind"] == "thread":
                sub["thread_anchor_page"] = max(1, last.total_pages - 1)
                sub["thread_total_pages"] = last.total_pages
            await self._commit(sub)
            async with self._lock:
                self._event(sub["id"], "info", "首次检查成功，已建立基线；历史内容不会推送")
            return sent
        max_pages = int(self._setting("max_pages_per_check", 20, 2, 100))
        page_number = max(1, int(sub.get("scan_page", 1)))
        prefetched = None
        if sub.get("scan_cutoff_time") is None:
            # Keep the user's original time boundary: deletions can move an
            # unseen new thread back to an already-read page during a scan.
            # The next sweep must still be able to find that older new item.
            sub["scan_cutoff_time"] = (sub.get("baseline_time") or sub["watermark_time"])
            sub["scan_cutoff_floor"] = sub["watermark_floor"]
            if sub["kind"] == "thread":
                first = await self.client.thread_page(sub["target"], 1)
                page_number = max(1, int(sub.get("thread_anchor_page", 1)))
                if first.total_pages < sub.get("thread_total_pages", 1) or page_number > first.total_pages:
                    page_number = 1
                anchor = first if page_number == 1 else await self.client.thread_page(sub["target"], page_number)
                # Ascending original-poster floors must still overlap the old
                # watermark. If deletions removed that overlap, rescan from 1.
                if page_number > 1 and not any(item.get("floor", 0) <= sub["scan_cutoff_floor"] for item in anchor.items):
                    page_number, anchor = 1, first
                prefetched = anchor
                sub["thread_total_pages"] = first.total_pages
                sub["scan_page"] = page_number
                await self._commit(sub)
        fingerprints = list(sub.get("scan_fingerprints", []))
        for _ in range(max_pages):
            if prefetched is not None:
                page, prefetched = prefetched, None
            else:
                page: Page = await (self.client.user_page(sub["target"], page_number) if sub["kind"] == "user"
                                    else self.client.thread_page(sub["target"], page_number))
            if sub["kind"] == "thread" and page.owner_id != sub.get("owner_id"):
                raise ValueError("帖子楼主与订阅基线不一致，未更新水位")
            sub["name"] = page.name or sub["name"]
            sub["title"] = page.title or sub["title"]
            fingerprint = hashlib.sha256(
                "|".join(item["key"] for item in page.items).encode()).hexdigest()
            repeated = fingerprint in fingerprints
            pending_keys = {item["key"] for item in sub["pending"]}
            for item in page.items:
                is_new = (item.get("floor", 0) > sub["scan_cutoff_floor"] if sub["kind"] == "thread"
                          else item.get("time", 0) >= sub["scan_cutoff_time"])
                if is_new and item["key"] not in sub["seen"] and item["key"] not in pending_keys:
                    if sub["kind"] == "user" and len(sub["seen"]) + len(sub["pending"]) >= MAX_USER_SEEN:
                        raise ValueError("用户订阅去重记录已达 10000 条，已保留数据并暂停推进；请取消后重新订阅以建立新基线")
                    if len(sub["pending"]) >= MAX_PENDING:
                        raise ValueError("待发送队列已达 1000 条，保留当前进度；请检查平台投递或降低订阅频率")
                    sub["pending"].append(copy.deepcopy(item))
                    pending_keys.add(item["key"])
            # Sorting before delivery keeps watermarks ordered inside this page.
            sub["pending"].sort(key=lambda item: (item.get("floor", 0) if sub["kind"] == "thread" else item.get("time", 0), item["key"]))
            await self._commit(sub)  # Persist outbox before any external side effect.
            sent += await self._drain(sub, budget - sent)
            if sub["pending"] or sent >= budget:
                sub["scan_page"] = page_number
                return sent
            fingerprints.append(fingerprint)
            if not page.has_more or not page.items or repeated:
                if sub["kind"] == "thread":
                    sub["thread_anchor_page"] = max(1, page.total_pages - 1)
                    sub["thread_total_pages"] = page.total_pages
                sub["scan_page"] = 1
                sub["scan_fingerprints"] = []
                sub["scan_cutoff_time"] = sub["scan_cutoff_floor"] = None
                return sent
            page_number += 1
            sub["scan_page"] = page_number
            sub["scan_fingerprints"] = fingerprints[-1000:]
            await self._commit(sub)
        async with self._lock:
            self._event(sub["id"], "info", f"本轮达到分页上限，下轮从第 {page_number} 页续查")
        return sent
