"""Security and config preservation tests for the standalone management API."""

import importlib.util
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch as mock_patch

from quart import Quart, g


spec = importlib.util.spec_from_file_location("alice_webui_under_test", Path(__file__).resolve().parents[1] / "webui.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ConfigSecurityTests(unittest.TestCase):
    def setUp(self):
        self.schema = {"tieba": {"type": "object", "items": {"cookie": {"type": "string"}}}, "proxy": {"type": "object", "items": {"address": {"type": "string"}}}, "monitoring": {"type": "object", "items": {"enabled": {"type": "bool"}, "poll_interval_seconds": {"type": "int", "min": 60}}}}
        self.config = {"tieba": {"cookie": "secret-cookie"}, "proxy": {"address": "http://username:password@proxy.example:8080"}, "monitoring": {"enabled": False, "poll_interval_seconds": 300}}

    def test_secrets_never_leave_config_api(self):
        sanitized, paths = module.sanitized_config(self.config)
        self.assertEqual(sanitized["tieba"]["cookie"], "")
        self.assertEqual(sanitized["proxy"]["address"], "")
        self.assertEqual(set(paths), {"tieba.cookie", "proxy.address"})
        self.assertEqual(self.config["tieba"]["cookie"], "secret-cookie")

    def test_redacted_blank_does_not_erase_credentials(self):
        updated = module.merge_config(self.config, {"tieba": {"cookie": ""}, "proxy": {"address": ""}, "monitoring": {"enabled": True}}, self.schema)
        self.assertEqual(updated["tieba"], self.config["tieba"])
        self.assertEqual(updated["proxy"], self.config["proxy"])
        self.assertTrue(updated["monitoring"]["enabled"])

    def test_clear_requires_known_secret_path(self):
        updated = module.merge_config(self.config, {}, self.schema, ["tieba.cookie"])
        self.assertEqual(updated["tieba"]["cookie"], "")
        with self.assertRaises(ValueError):
            module.merge_config(self.config, {}, self.schema, ["monitoring.enabled"])

    def test_rejects_unknown_fields_and_invalid_types(self):
        for patch in [{"other_plugin": True}, {"monitoring": {"enabled": "true"}}, {"monitoring": {"poll_interval_seconds": True}}, {"monitoring": {"poll_interval_seconds": 1}}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                module.merge_config(self.config, patch, self.schema)


class NativeBridgeContractTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the actual frontend bridge contract")
    def test_modern_and_legacy_frontend_paths_and_response_shapes(self):
        script = Path(__file__).with_name("test_alice_webui_bridge.cjs")
        result = subprocess.run([shutil.which("node"), str(script)], capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ManagementAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Explicitly model AstrBot 4.25. A newer installed SDK can expose
        # Starlette's API, which must not run inside this Quart test fixture.
        legacy_patch = mock_patch.dict(sys.modules, {"astrbot.api.web": None})
        legacy_patch.start()
        self.addCleanup(legacy_patch.stop)
        self.app = Quart(__name__)
        self.plugin = SimpleNamespace(context=SimpleNamespace(registered_web_apis=[]))
        self.webui = module.WebUI(self.plugin, {}, SimpleNamespace())

        @self.app.get("/api/plug/test")
        async def handler():
            return await self.webui.get_config()

        @self.app.post("/api/plug/test")
        async def post_handler():
            return await self.webui.save_config()

        @self.app.before_request
        async def fake_dashboard_auth():
            # Only emulate the identity already checked by AstrBot middleware.
            from quart import request
            if request.headers.get("X-Test-Login") == "yes":
                g.username = "admin"

    async def test_requires_dashboard_identity(self):
        response = await self.app.test_client().get("/api/plug/test")
        self.assertEqual(response.status_code, 401)
        self.assertEqual((await response.get_json())["status"], "error")

    async def test_logged_in_config_is_redacted(self):
        self.webui.config = {"tieba": {"cookie": "do-not-disclose"}}
        response = await self.app.test_client().get("/api/plug/test", headers={"X-Test-Login": "yes"})
        self.assertEqual(response.status_code, 200)
        payload = await response.get_json()
        self.assertEqual(payload["data"]["config"]["tieba"]["cookie"], "")

    async def test_rejects_cross_origin_mutation(self):
        response = await self.app.test_client().post("/api/plug/test", headers={"X-Test-Login": "yes", "Origin": "https://attacker.example"}, json={"config": {}})
        self.assertEqual(response.status_code, 401)

    async def test_close_does_not_remove_another_instance_route(self):
        own_handler, replacement_handler = self.webui.get_config, lambda: None
        self.webui._routes["/owned"] = own_handler
        self.plugin.context.registered_web_apis = [("/owned", replacement_handler, ["GET"], "new instance"), ("/other", lambda: None, ["GET"], "other plugin")]
        await self.webui.close()
        self.assertEqual(len(self.plugin.context.registered_web_apis), 2)

    async def test_close_before_registration_is_safe(self):
        self.plugin.context = SimpleNamespace()
        await self.webui.close()

    async def test_close_cancels_and_collects_active_http_request(self):
        entered = asyncio.Event()
        completed = asyncio.Event()

        async def fake_request():
            return SimpleNamespace()

        async def operation(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                completed.set()

        self.webui._request = fake_request
        self.webui._response = lambda data=None, error=None, status=200: (data, error, status)
        task = asyncio.create_task(self.webui._execute(operation))
        await entered.wait()
        await self.webui.close()
        self.assertTrue(task.cancelled())
        self.assertTrue(completed.is_set())
        self.assertFalse(self.webui._active_requests)
        result = await self.webui._execute(operation)
        self.assertEqual(result[2], 401)
        self.assertFalse(self.webui._active_requests)


class NativeManagementAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.official_web = ModuleType("astrbot.api.web")
        self.official_web.request = SimpleNamespace(username="admin", method="POST", headers={"Host": "astrbot.example", "Content-Length": "2"})

        async def body():
            return b"{}"

        self.official_web.request.body = body
        self.official_web.json_response = lambda data=None, status_code=200, headers=None: {"body": data, "status_code": status_code}
        native_patch = mock_patch.dict(sys.modules, {"astrbot.api.web": self.official_web})
        native_patch.start()
        self.addCleanup(native_patch.stop)
        plugin = SimpleNamespace(context=SimpleNamespace(registered_web_apis=[]))
        self.webui = module.WebUI(plugin, {"tieba": {"cookie": "private-cookie"}}, SimpleNamespace())

    async def test_native_context_requires_authenticated_username(self):
        self.official_web.request.username = None
        response = await self.webui.get_config()
        self.assertEqual(response["status_code"], 401)
        self.assertEqual(response["body"]["status"], "error")

    async def test_native_body_reads_bounded_json_and_redacts_config(self):
        request = await self.webui._request()
        self.assertEqual(await self.webui._body(request), {})
        response = await self.webui.get_config()
        self.assertEqual(response["status_code"], 200)
        self.assertEqual(response["body"]["data"]["config"]["tieba"]["cookie"], "")

    async def test_native_context_rejects_cross_origin_post(self):
        self.official_web.request.headers["Origin"] = "https://attacker.example"
        response = await self.webui.get_config()
        self.assertEqual(response["status_code"], 401)


if __name__ == "__main__":
    unittest.main()
