"""Authenticated management APIs for the native AstrBot plugin page.

The dashboard owns authentication and static page delivery. This module only
registers namespaced handlers; it never opens a second HTTP server.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
PREFIX = "/astrbot_plugin_alice_tieba/api"
SECRET_KEYS = {"cookie", "api_key", "token", "password", "secret", "access_token"}


def is_secret(path: tuple[str, ...], value: Any = None) -> bool:
    """Hide credentials, including credentials embedded in a proxy URL."""
    if path and (path[-1].lower() in SECRET_KEYS or path[-1].lower().endswith("_api_key")):
        return True
    if path == ("proxy", "address") and isinstance(value, str):
        try:
            parsed = urlsplit(value)
            return parsed.username is not None or parsed.password is not None
        except ValueError:
            return True
    return False


def sanitized_config(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    result = copy.deepcopy(dict(config))
    hidden: list[str] = []

    def visit(node: dict[str, Any], prefix: tuple[str, ...] = ()) -> None:
        for key, value in node.items():
            path = prefix + (key,)
            if is_secret(path, value):
                hidden.append(".".join(path))
                node[key] = ""
            elif isinstance(value, dict):
                visit(value, path)

    visit(result)
    return result, hidden


def validate_value(value: Any, spec: dict[str, Any], path: str) -> Any:
    """Validate API values against the plugin schema without implicit casts."""
    kind = spec.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} 必须是配置对象")
        children = spec.get("items", {})
        unknown = set(value) - set(children)
        if unknown:
            raise ValueError(f"{path} 含未知字段：{', '.join(sorted(unknown))}")
        return {key: validate_value(item, children[key], f"{path}.{key}") for key, item in value.items()}
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{path} 必须是开关")
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int) or abs(value) > 10**9:
            raise ValueError(f"{path} 必须是有效整数")
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 10**9:
            raise ValueError(f"{path} 必须是有限数值")
    elif kind in {"string", "text"}:
        if not isinstance(value, str) or len(value) > 100_000 or "\x00" in value:
            raise ValueError(f"{path} 必须是有效文本")
    elif kind in {"list", "file"}:
        if not isinstance(value, list) or len(value) > 1000:
            raise ValueError(f"{path} 必须是列表，最多 1000 项")
        item_spec = spec.get("items", {"type": "string"})
        return [validate_value(item, item_spec, f"{path}[{index}]") for index, item in enumerate(value)]
    elif kind == "template_list":
        if not isinstance(value, list) or len(value) > 100:
            raise ValueError(f"{path} 必须是模板列表")
        result = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(f"{path}[{index}] 必须是对象")
            template_key = item.get("__template_key")
            template = spec.get("templates", {}).get(template_key)
            if not template:
                raise ValueError(f"{path}[{index}] 模板不存在")
            row = validate_value({key: val for key, val in item.items() if key != "__template_key"}, {"type": "object", "items": template.get("items", {})}, f"{path}[{index}]")
            result.append({"__template_key": template_key, **row})
        return result
    else:
        raise ValueError(f"{path} 不支持此字段类型")
    if "options" in spec and value not in spec["options"]:
        raise ValueError(f"{path} 必须选择列表中的值")
    slider = spec.get("slider", {})
    if kind in {"int", "float"}:
        minimum, maximum = spec.get("min", slider.get("min")), spec.get("max", slider.get("max"))
        if minimum is not None and value < minimum or maximum is not None and value > maximum:
            raise ValueError(f"{path} 超出允许范围")
    return value


def merge_config(current: dict[str, Any], patch: dict[str, Any], schema: dict[str, Any], clear_secrets: list[str] | None = None) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise ValueError("配置内容必须是对象")
    validated = validate_value(patch, {"type": "object", "items": schema}, "配置")
    result = copy.deepcopy(dict(current))

    def merge(dest: dict[str, Any], changes: dict[str, Any], prefix: tuple[str, ...] = ()) -> None:
        for key, value in changes.items():
            path = prefix + (key,)
            if isinstance(value, dict):
                if not isinstance(dest.get(key), dict):
                    dest[key] = {}
                merge(dest[key], value, path)
            elif is_secret(path, dest.get(key)) and value == "":
                continue  # A redacted blank never erases an existing credential.
            else:
                dest[key] = value

    merge(result, validated)
    if clear_secrets is not None:
        if not isinstance(clear_secrets, list) or len(clear_secrets) > 20:
            raise ValueError("清空凭据列表无效")
        for dotted in clear_secrets:
            if not isinstance(dotted, str):
                raise ValueError("清空凭据路径无效")
            keys = tuple(dotted.split("."))
            node, schema_node = result, schema
            for key in keys[:-1]:
                if key not in schema_node or schema_node[key].get("type") != "object":
                    raise ValueError("清空凭据路径不存在")
                schema_node = schema_node[key].get("items", {})
                node = node.setdefault(key, {})
            if not keys or keys[-1] not in schema_node or not is_secret(keys, node.get(keys[-1])):
                raise ValueError("只能显式清空凭据字段")
            node[keys[-1]] = ""
    return result


async def maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class WebUI:
    """Register and remove this plugin's API handlers on load/unload."""

    def __init__(self, plugin: Any, config: Any, monitor: Any) -> None:
        self.plugin = plugin
        self.config = config
        self.monitor = monitor
        self.schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self._routes: dict[str, Any] = {}
        self._save_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._active_requests: set[asyncio.Task[Any]] = set()
        self._closed = False

    def register(self) -> None:
        self._closed = False
        endpoints = {
            "overview": (self.overview, ["GET"]),
            "schema": (self.get_schema, ["GET"]),
            "config": (self.get_config, ["GET"]),
            "config/save": (self.save_config, ["POST"]),
            "subscriptions": (self.subscriptions, ["GET", "POST"]),
            "subscription": (self.subscription, ["POST"]),
            "check": (self.check, ["POST"]),
            "events": (self.events, ["GET"]),
            "cache/clear": (self.clear_cache, ["POST"]),
        }
        for suffix, (handler, methods) in endpoints.items():
            route = f"{PREFIX}/{suffix}"
            self._routes[route] = handler
            self.plugin.context.register_web_api(route, handler, methods, "爱丽丝的贴吧工具管理")

    async def close(self) -> None:
        async with self._close_lock:
            self._closed = True
            routes = getattr(self.plugin.context, "registered_web_apis", None)
            if routes is not None:
                routes[:] = [row for row in routes if self._routes.get(row[0]) != row[1]]
            self._routes.clear()
            current = asyncio.current_task()
            active = [task for task in self._active_requests if task is not current]
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)

    async def _request(self) -> Any:
        try:
            from astrbot.api.web import request
            username = request.username
        except ImportError:
            from quart import g, request
            username = getattr(g, "username", None)
        if self._closed:
            raise PermissionError("插件正在重载，请稍后刷新")
        if not username:
            raise PermissionError("请从已登录的 AstrBot 控制台打开管理页面")
        origin = request.headers.get("Origin")
        if request.method == "POST" and origin:
            try:
                origin_host = urlsplit(origin).netloc.lower()
            except ValueError as exc:
                raise PermissionError("请求来源无效") from exc
            if not origin_host or origin_host != request.headers.get("Host", "").lower():
                raise PermissionError("请求来源与 AstrBot 控制台不一致")
        return request

    @staticmethod
    def _response(data: Any = None, error: str | None = None, status: int = 200) -> Any:
        try:
            from astrbot.api.web import json_response
        except ImportError:
            from quart import jsonify
            return jsonify({"status": "error" if error else "ok", "message": error or "", "data": data}), status
        return json_response({"status": "error" if error else "ok", "message": error or "", "data": data}, status_code=status)

    async def _execute(self, operation: Any) -> Any:
        if self._closed:
            return self._response(error="插件正在重载，请稍后刷新", status=401)
        current = asyncio.current_task()
        if current is not None:
            self._active_requests.add(current)
        try:
            req = await self._request()
            return self._response(await operation(req))
        except PermissionError as exc:
            return self._response(error=str(exc), status=401)
        except (ValueError, KeyError, TypeError) as exc:
            return self._response(error=str(exc), status=400)
        except Exception:
            # Do not expose exception strings that can contain cookies/URLs.
            logger = getattr(self.plugin, "logger", None)
            if logger is not None:
                logger.error("爱丽丝贴吧管理 API 操作失败，请检查插件运行状态")
            return self._response(error="操作未完成，请查看 AstrBot 日志并稍后重试", status=500)
        finally:
            if current is not None:
                self._active_requests.discard(current)

    async def _body(self, req: Any) -> dict[str, Any]:
        raw_length = req.headers.get("Content-Length", "0")
        if not str(raw_length).isdigit() or int(raw_length) > 512 * 1024:
            raise ValueError("请求内容过大")
        if hasattr(req, "get_json"):
            raw_body = await req.get_data()
        else:
            raw_body = await req.body()
        if len(raw_body) > 512 * 1024:
            raise ValueError("请求内容过大")
        try:
            body = json.loads(raw_body)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("请求必须是有效 JSON") from exc
        if not isinstance(body, dict):
            raise ValueError("请求必须是 JSON 对象")
        return body

    async def overview(self) -> Any:
        async def run(req: Any) -> Any:
            return {"monitor": await self.monitor.status(), "subscriptions": await self.monitor.list_subscriptions(), "events": await self.monitor.events(8), "cache_clear_available": hasattr(self.plugin, "clear_media_cache"), "name": "爱丽丝的贴吧工具"}
        return await self._execute(run)

    async def get_schema(self) -> Any:
        async def run(req: Any) -> Any:
            return self.schema
        return await self._execute(run)

    async def get_config(self) -> Any:
        async def run(req: Any) -> Any:
            config, secrets = sanitized_config(self.config)
            return {"config": config, "secret_paths": secrets}
        return await self._execute(run)

    async def save_config(self) -> Any:
        async def run(req: Any) -> Any:
            body = await self._body(req)
            async with self._save_lock:
                updated = merge_config(self.config, body.get("config", {}), self.schema, body.get("clear_secret_paths"))
                previous = copy.deepcopy(dict(self.config))
                self.config.clear()
                self.config.update(updated)
                try:
                    await maybe_await(self.config.save_config())
                except BaseException:
                    self.config.clear()
                    self.config.update(previous)
                    raise
            return {"saved": True, "reload_required": True}
        return await self._execute(run)

    async def subscriptions(self) -> Any:
        async def run(req: Any) -> Any:
            if req.method == "GET":
                return await self.monitor.list_subscriptions()
            body = await self._body(req)
            allowed = {"kind", "target", "session", "label", "keywords", "enabled"}
            if set(body) - allowed:
                raise ValueError("订阅含未知字段")
            return await self.monitor.add_subscription(**body)
        return await self._execute(run)

    async def subscription(self) -> Any:
        async def run(req: Any) -> Any:
            body = await self._body(req)
            identifier = body.get("id")
            if not isinstance(identifier, str) or len(identifier) > 100:
                raise ValueError("订阅 ID 无效")
            if body.get("action") == "delete":
                return await self.monitor.remove_subscription(identifier)
            patch = body.get("patch")
            if not isinstance(patch, dict) or set(patch) - {"enabled", "label", "keywords", "session"}:
                raise ValueError("仅可修改启用状态、名称、关键词和接收会话")
            return await self.monitor.update_subscription(identifier, patch)
        return await self._execute(run)

    async def check(self) -> Any:
        async def run(req: Any) -> Any:
            body = await self._body(req)
            identifier = body.get("id")
            if identifier is not None and (not isinstance(identifier, str) or len(identifier) > 100):
                raise ValueError("订阅 ID 无效")
            return await self.monitor.check_now(identifier)
        return await self._execute(run)

    async def events(self) -> Any:
        async def run(req: Any) -> Any:
            return await self.monitor.events(100)
        return await self._execute(run)

    async def clear_cache(self) -> Any:
        async def run(req: Any) -> Any:
            body = await self._body(req)
            if body.get("confirm") is not True:
                raise ValueError("请先确认清理缓存")
            if not hasattr(self.plugin, "clear_media_cache"):
                raise ValueError("当前版本不支持页面清理缓存")
            return await self.plugin.clear_media_cache()
        return await self._execute(run)
