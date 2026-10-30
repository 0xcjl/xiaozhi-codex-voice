import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


class ConsoleConfigLoaderTests(unittest.IsolatedAsyncioTestCase):
    def load(self, remote):
        client = types.ModuleType("config.manage_api_client")
        client.init_service = Mock()
        client.get_server_config = AsyncMock(return_value=remote)
        client.get_agent_models = AsyncMock()
        client.get_correct_words = AsyncMock()
        client.DeviceNotFoundException = type("DeviceNotFoundException", (Exception,), {})
        client.DeviceBindException = type("DeviceBindException", (Exception,), {})
        path = Path(__file__).resolve().parents[4] / "config/config_loader.py"
        spec = importlib.util.spec_from_file_location("isolated_config_loader", path)
        module = importlib.util.module_from_spec(spec)
        # Only unrelated YAML loading and network imports are substituted.
        with patch.dict(sys.modules, {"yaml": types.ModuleType("yaml"), "config.manage_api_client": client}):
            spec.loader.exec_module(module)
        return module

    async def test_console_cannot_grant_access_when_host_has_not_enabled_it(self):
        module = self.load({"server": {}, "codex_realtime": {"enabled": True, "allowed_user_ids": [99]}})
        result = await module.get_config_from_api_async({"manager-api": {"url": "local", "secret": "test"}})
        self.assertEqual(result["codex_realtime"], {"enabled": False})

    async def test_host_workspace_and_grants_take_precedence_over_console_parameters(self):
        host = {"enabled": True, "console_selection": True, "workspace": "/host/path", "allowed_user_ids": [1]}
        module = self.load({"server": {}, "codex_realtime": {"workspace": "/other", "allowed_user_ids": [99]}})
        result = await module.get_config_from_api_async({"manager-api": {"url": "local", "secret": "test"}, "codex_realtime": host})
        self.assertEqual(result["codex_realtime"], host)
