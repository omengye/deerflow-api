import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.dependencies import ClientManager
from app.middleware import ApiKeyAuthMiddleware
from app.routers import admin as admin_router
from deerflow.config.app_config import reset_app_config
from deerflow.config.extensions_config import reset_extensions_config


class AdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_auth_enabled = settings.auth_enabled
        self._original_api_keys = list(settings.api_keys)
        self._original_config_path = settings.config_path
        self._original_runtime = {
            "model_name": settings.model_name,
            "thinking_enabled": settings.thinking_enabled,
            "subagent_enabled": settings.subagent_enabled,
            "plan_mode": settings.plan_mode,
            "max_concurrent_subagents": settings.max_concurrent_subagents,
            "chat_request_timeout": settings.chat_request_timeout,
            "max_upload_size_mb": settings.max_upload_size_mb,
            "max_uploads_per_request": settings.max_uploads_per_request,
            "allowed_upload_extensions": list(settings.allowed_upload_extensions),
        }
        self._original_feishu = settings.feishu
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmp.name) / "config.yaml"
        self.skills_root = Path(self._tmp.name) / "skills"
        self.skills_root.joinpath("public", "builtin").mkdir(parents=True)
        self.skills_root.joinpath("custom").mkdir(parents=True)
        self.skills_root.joinpath("public", "builtin", "SKILL.md").write_text(
            "---\nname: builtin\ndescription: Built in skill\n---\n",
            encoding="utf-8",
        )
        self.extensions_path = Path(self._tmp.name) / "extensions_config.json"
        self.extensions_path.write_text(
            """
{
  "mcpServers": {
    "local": {
      "enabled": true,
      "type": "stdio",
      "command": "python",
      "args": ["--version"],
      "env": {
        "SECRET_TOKEN": "literal-secret"
      }
    }
  },
  "skills": {}
}
""".strip(),
            encoding="utf-8",
        )
        skills_path = str(self.skills_root).replace("\\", "/")
        extensions_path = str(self.extensions_path).replace("\\", "/")
        self.config_path.write_text(
            f"""
config_version: 12
api:
  auth_enabled: true
  api_keys:
    - secret
  chat_request_timeout: 600
  max_concurrent_subagents: 3
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
models:
  - name: base
    display_name: Base Model
    use: langchain_openai:ChatOpenAI
    model: base-model
    api_key: literal-secret
    supports_thinking: false
    supports_vision: true
default_model: base
skills:
  enabled: true
  path: {skills_path}
  extensions_file: {extensions_path}
tools: []
tool_groups: []
""".strip(),
            encoding="utf-8",
        )
        settings.auth_enabled = True
        settings.api_keys = ["secret"]
        settings.config_path = str(self.config_path)
        reset_app_config()
        reset_extensions_config()

    def tearDown(self) -> None:
        settings.auth_enabled = self._original_auth_enabled
        settings.api_keys = self._original_api_keys
        settings.config_path = self._original_config_path
        for key, value in self._original_runtime.items():
            setattr(settings, key, value)
        settings.feishu = self._original_feishu
        reset_app_config()
        reset_extensions_config()
        self._tmp.cleanup()

    def _client(self) -> TestClient:
        app = FastAPI()
        app.add_middleware(ApiKeyAuthMiddleware)
        app.include_router(admin_router.router, prefix="/api")
        return TestClient(app)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer secret"}

    def test_admin_routes_require_authentication(self) -> None:
        client = self._client()

        unauthorized = client.get("/api/admin/me")
        authorized = client.get("/api/admin/me", headers=self._auth_headers())

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(authorized.status_code, 200)
        self.assertTrue(authorized.json()["capabilities"]["models_write"])

    def test_admin_api_is_disabled_when_auth_is_disabled(self) -> None:
        settings.auth_enabled = False
        client = self._client()

        response = client.get("/api/admin/me")

        self.assertEqual(response.status_code, 404)

    def test_admin_config_redacts_literal_secrets(self) -> None:
        client = self._client()

        response = client.get("/api/admin/config", headers=self._auth_headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["models"][0]["api_key"]["redacted"])
        self.assertTrue(payload["api"]["api_keys"]["redacted"])

    def test_update_models_preserves_redacted_existing_secret(self) -> None:
        client = self._client()
        config_response = client.get("/api/admin/config", headers=self._auth_headers())
        model = config_response.json()["models"][0]
        model["display_name"] = "Renamed Model"

        response = client.put(
            "/api/admin/models",
            headers=self._auth_headers(),
            json={"models": [model], "default_model": "base", "reload": False},
        )

        self.assertEqual(response.status_code, 200)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["models"][0]["display_name"], "Renamed Model")
        self.assertEqual(raw["models"][0]["api_key"], "literal-secret")

    def test_update_models_rejects_duplicate_names(self) -> None:
        client = self._client()
        model = {
            "name": "dup",
            "display_name": "Dup",
            "use": "langchain_openai:ChatOpenAI",
            "model": "dup-model",
            "api_key": "secret",
        }

        response = client.put(
            "/api/admin/models",
            headers=self._auth_headers(),
            json={"models": [model, model], "default_model": "dup", "reload": False},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Duplicate model name", response.json()["detail"])

    def test_reload_clears_cached_clients(self) -> None:
        client = self._client()
        manager = ClientManager()
        manager._client_map[("sync",)] = object()
        manager._async_client_map[("async",)] = object()

        with patch.object(admin_router, "get_client_manager", return_value=manager):
            response = client.post(
                "/api/admin/config/reload",
                headers=self._auth_headers(),
                json={"include_extensions": True, "reset_clients": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["clients_reset"])
        self.assertEqual(manager._client_map, {})
        self.assertEqual(manager._async_client_map, {})

    def test_custom_skill_crud_history_and_support_files(self) -> None:
        client = self._client()
        content = "---\nname: market-research\ndescription: Market research\n---\n\n## Workflow\n- Search.\n"

        created = client.put(
            "/api/admin/skills/custom/market-research",
            headers=self._auth_headers(),
            json={"content": content, "enabled": False, "reload": False},
        )
        self.assertEqual(created.status_code, 200)
        self.assertFalse(created.json()["enabled"])
        self.assertTrue(self.skills_root.joinpath("custom", "market-research", "SKILL.md").exists())

        read = client.get("/api/admin/skills/custom/market-research", headers=self._auth_headers())
        self.assertEqual(read.status_code, 200)
        self.assertIn("Market research", read.json()["content"])

        support = client.put(
            "/api/admin/skills/custom/market-research/files/references/notes.md",
            headers=self._auth_headers(),
            json={"content": "notes", "reload": False},
        )
        self.assertEqual(support.status_code, 200)
        self.assertTrue(self.skills_root.joinpath("custom", "market-research", "references", "notes.md").exists())

        history = client.get("/api/admin/skills/custom/market-research/history", headers=self._auth_headers())
        self.assertEqual(history.status_code, 200)
        actions = [item["action"] for item in history.json()["history"]]
        self.assertIn("create", actions)
        self.assertIn("write_file", actions)
        self.assertNotIn("new_content", history.json()["history"][0])

        deleted_file = client.delete(
            "/api/admin/skills/custom/market-research/files/references/notes.md",
            headers=self._auth_headers(),
        )
        self.assertEqual(deleted_file.status_code, 200)

        deleted = client.delete("/api/admin/skills/custom/market-research", headers=self._auth_headers())
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(self.skills_root.joinpath("custom", "market-research").exists())

    def test_custom_skill_rejects_builtin_name_and_bad_support_path(self) -> None:
        client = self._client()
        content = "---\nname: builtin\ndescription: Bad override\n---\n"

        override = client.put(
            "/api/admin/skills/custom/builtin",
            headers=self._auth_headers(),
            json={"content": content, "reload": False},
        )
        self.assertEqual(override.status_code, 400)

        create = client.put(
            "/api/admin/skills/custom/local-only",
            headers=self._auth_headers(),
            json={"content": "---\nname: local-only\ndescription: Local\n---\n", "reload": False},
        )
        self.assertEqual(create.status_code, 200)

        bad_file = client.put(
            "/api/admin/skills/custom/local-only/files/not-allowed/file.md",
            headers=self._auth_headers(),
            json={"content": "bad", "reload": False},
        )
        self.assertEqual(bad_file.status_code, 400)

    def test_runtime_patch_writes_config_and_hot_applies_settings(self) -> None:
        client = self._client()

        response = client.patch(
            "/api/admin/runtime",
            headers=self._auth_headers(),
            json={
                "model_name": "base",
                "thinking_enabled": False,
                "max_concurrent_subagents": 2,
                "allowed_upload_extensions": ["txt", ".PDF", "txt"],
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["effects"]["thinking_enabled"], "hot_applied")
        self.assertEqual(settings.model_name, "base")
        self.assertFalse(settings.thinking_enabled)
        self.assertEqual(settings.max_concurrent_subagents, 2)
        self.assertEqual(settings.allowed_upload_extensions, [".pdf", ".txt"])
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["api"]["allowed_upload_extensions"], [".pdf", ".txt"])

    def test_feishu_config_write_redacts_and_preserves_secrets(self) -> None:
        client = self._client()

        created = client.put(
            "/api/admin/feishu",
            headers=self._auth_headers(),
            json={
                "enabled": True,
                "app_id": "cli_test",
                "app_secret": "literal-feishu-secret",
                "verification_token": "literal-token",
                "restart": False,
            },
        )

        self.assertEqual(created.status_code, 200)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["api"]["feishu"]["app_id"], "cli_test")
        self.assertEqual(raw["api"]["feishu"]["app_secret"], "literal-feishu-secret")

        read = client.get("/api/admin/feishu", headers=self._auth_headers())
        self.assertEqual(read.status_code, 200)
        payload = read.json()
        self.assertTrue(payload["config"]["app_secret"]["redacted"])
        self.assertTrue(payload["config"]["verification_token"]["redacted"])

        updated_config = payload["config"]
        updated_config["app_id"] = "cli_renamed"
        preserved = client.put(
            "/api/admin/feishu",
            headers=self._auth_headers(),
            json={**updated_config, "restart": False},
        )

        self.assertEqual(preserved.status_code, 200)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["api"]["feishu"]["app_id"], "cli_renamed")
        self.assertEqual(raw["api"]["feishu"]["app_secret"], "literal-feishu-secret")
        self.assertEqual(raw["api"]["feishu"]["verification_token"], "literal-token")

    def test_mcp_admin_enable_disable_and_test_redacts_secrets(self) -> None:
        client = self._client()

        tested = client.post("/api/admin/mcp/local/test", headers=self._auth_headers(), json={"timeout_seconds": 1})
        self.assertEqual(tested.status_code, 200)
        self.assertTrue(tested.json()["success"])
        self.assertTrue(tested.json()["server"]["env"]["SECRET_TOKEN"]["redacted"])

        disabled = client.post("/api/admin/mcp/local/disable", headers=self._auth_headers())
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["enabled"])
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertIn("models", raw)
        ext = yaml.safe_load(self.extensions_path.read_text(encoding="utf-8"))
        self.assertFalse(ext["mcpServers"]["local"]["enabled"])

        enabled = client.post("/api/admin/mcp/local/enable", headers=self._auth_headers())
        self.assertEqual(enabled.status_code, 200)
        ext = yaml.safe_load(self.extensions_path.read_text(encoding="utf-8"))
        self.assertTrue(ext["mcpServers"]["local"]["enabled"])


if __name__ == "__main__":
    unittest.main()
