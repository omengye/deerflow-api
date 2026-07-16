import asyncio
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
from deerflow.runtime.scheduler import SchedulerStore


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
    when_thinking_enabled:
      extra_body:
        enable_thinking: true
default_model: base
title:
  enabled: true
  model_name: base
subagents:
  enabled: true
  timeout_seconds: 900
  agents: {{}}
  custom_agents: {{}}
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

    def test_bulk_update_models_preserves_omitted_api_key(self) -> None:
        client = self._client()
        config_response = client.get("/api/admin/config", headers=self._auth_headers())
        model = config_response.json()["models"][0]
        model.pop("api_key")
        model["display_name"] = "No Secret In Payload"

        response = client.put(
            "/api/admin/models",
            headers=self._auth_headers(),
            json={"models": [model], "default_model": "base", "reload": False},
        )

        self.assertEqual(response.status_code, 200)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["models"][0]["api_key"], "literal-secret")

    def test_patch_model_blank_api_key_preserves_secret_and_advanced_fields(self) -> None:
        client = self._client()

        response = client.patch(
            "/api/admin/models/base",
            headers=self._auth_headers(),
            json={
                "changes": {"name": "renamed", "display_name": "Renamed", "api_key": ""},
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["models"][0]["name"], "renamed")
        self.assertEqual(raw["models"][0]["api_key"], "literal-secret")
        self.assertTrue(raw["models"][0]["when_thinking_enabled"]["extra_body"]["enable_thinking"])
        self.assertEqual(raw["default_model"], "renamed")

    def test_patch_model_requires_explicit_clear_to_remove_api_key(self) -> None:
        client = self._client()

        response = client.patch(
            "/api/admin/models/base",
            headers=self._auth_headers(),
            json={"clear_api_key": True, "reload": False},
        )

        self.assertEqual(response.status_code, 200)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", raw["models"][0])

    def test_patch_model_preserves_environment_reference_api_key(self) -> None:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw["models"][0]["api_key"] = "${BASE_MODEL_KEY:-test-fallback}"
        self.config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        client = self._client()

        response = client.patch(
            "/api/admin/models/base",
            headers=self._auth_headers(),
            json={"changes": {"display_name": "Env Model"}, "reload": False},
        )

        self.assertEqual(response.status_code, 200)
        updated = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["models"][0]["api_key"], "${BASE_MODEL_KEY:-test-fallback}")

    def test_create_set_default_and_delete_model(self) -> None:
        client = self._client()
        created = client.post(
            "/api/admin/models",
            headers=self._auth_headers(),
            json={
                "model": {
                    "name": "second",
                    "display_name": "Second",
                    "use": "langchain_openai:ChatOpenAI",
                    "model": "second-model",
                    "api_key": "second-secret",
                },
                "set_default": True,
                "reload": False,
            },
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["default_model"], "second")

        deleted = client.delete("/api/admin/models/second?reload=false", headers=self._auth_headers())
        self.assertEqual(deleted.status_code, 200)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["default_model"], "base")
        self.assertEqual([model["name"] for model in raw["models"]], ["base"])

    def test_title_and_subagents_section_updates(self) -> None:
        client = self._client()

        title = client.put(
            "/api/admin/title",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "max_words": 8,
                    "max_chars": 80,
                    "model_name": "base",
                    "prompt_template": "Title {max_words}: {user_msg} / {assistant_msg}",
                },
                "reload": False,
            },
        )
        self.assertEqual(title.status_code, 200)

        subagents = client.put(
            "/api/admin/subagents",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "timeout_seconds": 600,
                    "max_turns": 30,
                    "agents": {"general-purpose": {"description": "Built-in override", "model": "base", "max_turns": 20}},
                    "custom_agents": {},
                },
                "reload": False,
            },
        )
        self.assertEqual(subagents.status_code, 200)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["title"]["max_words"], 8)
        self.assertEqual(raw["subagents"]["agents"]["general-purpose"]["model"], "base")
        self.assertEqual(raw["subagents"]["agents"]["general-purpose"]["description"], "Built-in override")

    def test_title_and_subagents_reject_unknown_models(self) -> None:
        client = self._client()
        title = client.put(
            "/api/admin/title",
            headers=self._auth_headers(),
            json={"config": {"enabled": True, "model_name": "missing"}, "reload": False},
        )
        self.assertEqual(title.status_code, 400)

        subagents = client.put(
            "/api/admin/subagents",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "agents": {"general-purpose": {"model": "missing"}},
                    "custom_agents": {},
                },
                "reload": False,
            },
        )
        self.assertEqual(subagents.status_code, 400)

    def test_config_health_reports_safe_contract_warnings(self) -> None:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw["guardrails"] = {"enabled": False, "providers": []}
        raw["tracing"] = {"enabled": False}
        raw["title"]["model"] = raw["title"].pop("model_name")
        self.config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        client = self._client()

        response = client.get("/api/admin/config/health", headers=self._auth_headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["valid"])
        codes = {warning["code"] for warning in payload["warnings"]}
        self.assertIn("legacy_title_model", codes)
        self.assertIn("guardrails_provider_contract", codes)
        self.assertIn("tracing_top_level_enabled_ignored", codes)
        self.assertGreaterEqual(payload["literal_secrets"]["count"], 1)

    def test_memory_and_summarization_section_updates(self) -> None:
        client = self._client()
        memory = client.put(
            "/api/admin/memory",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "storage_path": "memory.json",
                    "storage_class": "deerflow.agents.memory.storage.FileMemoryStorage",
                    "debounce_seconds": 15,
                    "model_name": "base",
                    "max_facts": 120,
                    "fact_confidence_threshold": 0.8,
                    "injection_enabled": True,
                    "max_injection_tokens": 2500,
                },
                "reload": False,
            },
        )
        self.assertEqual(memory.status_code, 200)

        summarization = client.put(
            "/api/admin/summarization",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "model_name": "base",
                    "trigger": [{"type": "messages", "value": 50}, {"type": "fraction", "value": 0.8}],
                    "keep": {"type": "messages", "value": 20},
                    "trim_tokens_to_summarize": 4000,
                    "summary_prompt": "Summarize the conversation.",
                    "preserve_recent_skill_count": 4,
                    "preserve_recent_skill_tokens": 20000,
                    "preserve_recent_skill_tokens_per_skill": 4000,
                    "skill_file_read_tool_names": ["read_file", "view"],
                },
                "reload": False,
            },
        )
        self.assertEqual(summarization.status_code, 200)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["memory"]["model_name"], "base")
        self.assertEqual(raw["memory"]["max_facts"], 120)
        self.assertEqual(raw["summarization"]["trigger"][1]["type"], "fraction")
        self.assertEqual(raw["summarization"]["keep"]["value"], 20)

    def test_memory_and_summarization_reject_invalid_references_and_thresholds(self) -> None:
        client = self._client()
        memory = client.put(
            "/api/admin/memory",
            headers=self._auth_headers(),
            json={"config": {"enabled": True, "model_name": "missing"}, "reload": False},
        )
        self.assertEqual(memory.status_code, 400)

        summarization = client.put(
            "/api/admin/summarization",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "model_name": "base",
                    "trigger": {"type": "fraction", "value": 1.5},
                    "keep": {"type": "messages", "value": 20},
                },
                "reload": False,
            },
        )
        self.assertEqual(summarization.status_code, 400)

    def test_scheduled_tasks_can_be_listed_and_deleted(self) -> None:
        db_path = Path(self._tmp.name) / "scheduled_tasks.db"
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw["api"]["scheduler_enabled"] = False
        raw["api"]["scheduler_db_path"] = str(db_path).replace("\\", "/")
        self.config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        store = SchedulerStore(db_path)
        store.setup()
        task = asyncio.run(
            store.create_task(
                thread_id="thread-admin",
                prompt="Generate the daily report",
                schedule_type="daily",
                schedule_expr={"time_of_day": "09:00"},
                timezone="Asia/Shanghai",
                metadata={"delivery": {"channel": "feishu", "chat_id": "secret-chat"}},
                kwargs={"internal": "value"},
            )
        )
        client = self._client()

        listed = client.get("/api/admin/scheduled-tasks", headers=self._auth_headers())

        self.assertEqual(listed.status_code, 200)
        payload = listed.json()
        self.assertFalse(payload["scheduler_enabled"])
        self.assertTrue(payload["storage_exists"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["tasks"][0]["id"], task.id)
        self.assertEqual(payload["tasks"][0]["prompt"], "Generate the daily report")
        self.assertNotIn("metadata", payload["tasks"][0])
        self.assertNotIn("kwargs", payload["tasks"][0])

        deleted = client.delete(f"/api/admin/scheduled-tasks/{task.id}", headers=self._auth_headers())
        missing = client.delete(f"/api/admin/scheduled-tasks/{task.id}", headers=self._auth_headers())

        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["deleted"], task.id)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(asyncio.run(store.list_tasks(include_disabled=True)), [])

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
