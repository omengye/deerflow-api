import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.dependencies import ClientManager
from app.middleware import ApiKeyAuthMiddleware
from app.routers import admin as admin_router
from deerflow.config.app_config import reset_app_config
from deerflow.config.extensions_config import reset_extensions_config
from deerflow.config.memory_config import (
    MemoryConfig,
    get_memory_config,
    set_memory_config,
)
from deerflow.runtime.scheduler import SchedulerStore
from deerflow.skills.evolution import (
    EvolutionSignal,
    SkillEvolutionService,
    SkillProposal,
    get_evolution_store,
)
from deerflow.skills.evolution.store import utc_now_iso
from deerflow.skills.security_scanner import ScanResult


class AdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_auth_enabled = settings.auth_enabled
        self._original_api_keys = list(settings.api_keys)
        self._original_config_path = settings.config_path
        self._original_checkpointer_type = settings.checkpointer_type
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
        self._original_thread_cleanup = settings.thread_cleanup.model_copy(deep=True)
        self._original_memory_config = get_memory_config().model_copy(deep=True)
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmp.name) / "config.yaml"
        self.skills_root = Path(self._tmp.name) / "skills"
        self.evolution_root = Path(self._tmp.name) / "skill-evolution"
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
      "command": "npx",
      "args": ["@example/mcp-server"],
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
        evolution_path = str(self.evolution_root).replace("\\", "/")
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
skill_evolution:
  enabled: false
  mode: review
  storage_path: {evolution_path}
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
        settings.checkpointer_type = self._original_checkpointer_type
        for key, value in self._original_runtime.items():
            setattr(settings, key, value)
        settings.feishu = self._original_feishu
        settings.thread_cleanup = self._original_thread_cleanup
        set_memory_config(self._original_memory_config)
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

    def test_admin_config_does_not_redact_model_token_limits(self) -> None:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw["models"][0].update(
            {
                "max_tokens": 8192,
                "profile": {
                    "max_input_tokens": 1_000_000,
                    "max_output_tokens": 65_536,
                    "access_token": "profile-secret",
                },
            }
        )
        self.config_path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        reset_app_config()
        client = self._client()

        response = client.get("/api/admin/config", headers=self._auth_headers())

        self.assertEqual(response.status_code, 200)
        model = response.json()["models"][0]
        self.assertEqual(model["max_tokens"], 8192)
        self.assertEqual(model["profile"]["max_input_tokens"], 1_000_000)
        self.assertEqual(model["profile"]["max_output_tokens"], 65_536)
        self.assertTrue(model["profile"]["access_token"]["redacted"])

    def test_admin_config_summary_reports_acp_timeout(self) -> None:
        """The ACP overview must surface timeout_seconds.

        The summary is built from an explicit field allowlist, so a newly added
        ACPAgentConfig field is invisible to the admin UI until it is listed
        there. Operators need this one to tell a hung agent (no timeout) from a
        slow one, and an explicit ``null`` -- "wait indefinitely" -- has to stay
        distinguishable from the 600s default rather than being coerced to it.
        """
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw["acp_agents"] = {
            "codex": {"command": "codex-acp", "description": "Codex via ACP", "timeout_seconds": 120},
            "claude_code": {"command": "claude-agent-acp", "description": "Claude Code via ACP"},
            "unbounded": {"command": "other-acp", "description": "No timeout", "timeout_seconds": None},
        }
        self.config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        reset_app_config()
        client = self._client()

        response = client.get("/api/admin/config", headers=self._auth_headers())

        self.assertEqual(response.status_code, 200)
        agents = {agent["name"]: agent for agent in response.json()["system_summary"]["acp_agents"]}
        self.assertEqual(agents["codex"]["timeout_seconds"], 120)
        # Omitted in config.yaml -> report the ACPAgentConfig default, not None.
        self.assertEqual(agents["claude_code"]["timeout_seconds"], 600)
        # Explicit null means unbounded and must not be reported as the default.
        self.assertIsNone(agents["unbounded"]["timeout_seconds"])

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
        self.assertTrue(subagents.json()["success"])
        self.assertIsNone(subagents.json()["reload"])
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["title"]["max_words"], 8)
        self.assertEqual(raw["subagents"]["agents"]["general-purpose"]["model"], "base")
        self.assertEqual(raw["subagents"]["agents"]["general-purpose"]["description"], "Built-in override")

    def test_get_subagents_includes_builtin_catalog_for_visual_editor(self) -> None:
        client = self._client()

        response = client.get("/api/admin/subagents", headers=self._auth_headers())

        self.assertEqual(response.status_code, 200)
        catalog = {agent["name"]: agent for agent in response.json()["builtin_agents"]}
        self.assertEqual(set(catalog), {"general-purpose", "bash"})
        self.assertEqual(catalog["general-purpose"]["default_model"], "inherit")
        self.assertTrue(catalog["bash"]["description"])

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

    def test_subagents_reject_unsupported_thinking_enabled(self) -> None:
        client = self._client()

        response = client.put(
            "/api/admin/subagents",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    # "base" declares supports_thinking: false in setUp's fixture config.
                    "agents": {"general-purpose": {"model": "base", "thinking_enabled": True}},
                    "custom_agents": {},
                },
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("supports_thinking", response.json()["detail"])

    def test_subagents_reject_unsupported_reasoning_effort(self) -> None:
        client = self._client()

        response = client.put(
            "/api/admin/subagents",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    # "base" has no supports_reasoning_effort in setUp's fixture config (defaults False).
                    "agents": {"general-purpose": {"model": "base", "reasoning_effort": "medium"}},
                    "custom_agents": {},
                },
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("supports_reasoning_effort", response.json()["detail"])

    def test_subagents_reject_invalid_reasoning_effort_value(self) -> None:
        client = self._client()

        response = client.put(
            "/api/admin/subagents",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    # No model override -> model can't be resolved, but the value-domain
                    # check still applies regardless of whether a model is resolvable.
                    "agents": {"general-purpose": {"reasoning_effort": "not-a-real-value"}},
                    "custom_agents": {},
                },
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("reasoning_effort", response.json()["detail"])

    def test_subagents_generation_overrides_allowed_when_model_inherited(self) -> None:
        client = self._client()

        response = client.put(
            "/api/admin/subagents",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    # No "model" key -> agent inherits parent's model, which isn't known
                    # statically here. thinking_enabled/reasoning_effort must be allowed
                    # through; models/factory.py degrades gracefully at run time instead.
                    "agents": {"general-purpose": {"thinking_enabled": True, "reasoning_effort": "medium"}},
                    "custom_agents": {},
                },
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        agent_raw = raw["subagents"]["agents"]["general-purpose"]
        self.assertTrue(agent_raw["thinking_enabled"])
        self.assertEqual(agent_raw["reasoning_effort"], "medium")

    def test_subagents_generation_overrides_accepted_for_capable_model(self) -> None:
        client = self._client()
        config_response = client.get("/api/admin/config", headers=self._auth_headers())
        model = config_response.json()["models"][0]
        model["supports_thinking"] = True
        model["supports_reasoning_effort"] = True
        models_update = client.put(
            "/api/admin/models",
            headers=self._auth_headers(),
            json={"models": [model], "default_model": "base", "reload": False},
        )
        self.assertEqual(models_update.status_code, 200)

        response = client.put(
            "/api/admin/subagents",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "agents": {
                        "general-purpose": {
                            "model": "base",
                            "thinking_enabled": True,
                            "reasoning_effort": "medium",
                            "model_settings": {"temperature": 0.5, "max_tokens": 1000},
                        }
                    },
                    "custom_agents": {},
                },
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        agent_raw = raw["subagents"]["agents"]["general-purpose"]
        self.assertTrue(agent_raw["thinking_enabled"])
        self.assertEqual(agent_raw["reasoning_effort"], "medium")
        self.assertEqual(agent_raw["model_settings"]["temperature"], 0.5)
        self.assertEqual(agent_raw["model_settings"]["max_tokens"], 1000)

    def test_subagents_reasoning_effort_minimal_allowed_for_non_codex_model(self) -> None:
        client = self._client()
        config_response = client.get("/api/admin/config", headers=self._auth_headers())
        model = config_response.json()["models"][0]
        model["supports_reasoning_effort"] = True
        models_update = client.put(
            "/api/admin/models",
            headers=self._auth_headers(),
            json={"models": [model], "default_model": "base", "reload": False},
        )
        self.assertEqual(models_update.status_code, 200)

        # "base" resolves to langchain_openai:ChatOpenAI, a non-Codex model.
        # "minimal" is a valid OpenAI-style reasoning_effort value for those
        # (models/factory.py:143 sets it directly), even though it's outside
        # the Codex-only value set.
        response = client.put(
            "/api/admin/subagents",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "agents": {"general-purpose": {"model": "base", "reasoning_effort": "minimal"}},
                    "custom_agents": {},
                },
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 200)

    def test_subagents_reasoning_effort_xhigh_rejected_for_non_codex_model(self) -> None:
        client = self._client()
        config_response = client.get("/api/admin/config", headers=self._auth_headers())
        model = config_response.json()["models"][0]
        model["supports_reasoning_effort"] = True
        models_update = client.put(
            "/api/admin/models",
            headers=self._auth_headers(),
            json={"models": [model], "default_model": "base", "reload": False},
        )
        self.assertEqual(models_update.status_code, 200)

        # "xhigh" is Codex-only (models/factory.py:179); "base" is a
        # non-Codex (ChatOpenAI) model, so this must be rejected even though
        # supports_reasoning_effort is true.
        response = client.put(
            "/api/admin/subagents",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "agents": {"general-purpose": {"model": "base", "reasoning_effort": "xhigh"}},
                    "custom_agents": {},
                },
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("reasoning_effort", response.json()["detail"])

    def test_subagents_reasoning_effort_xhigh_allowed_for_codex_model(self) -> None:
        client = self._client()
        config_response = client.get("/api/admin/config", headers=self._auth_headers())
        base_model = config_response.json()["models"][0]
        codex_model = {
            "name": "codex-model",
            "use": "deerflow.models.openai_codex_provider:CodexChatModel",
            "model": "gpt-5.4",
            "supports_reasoning_effort": True,
        }
        models_update = client.put(
            "/api/admin/models",
            headers=self._auth_headers(),
            json={"models": [base_model, codex_model], "default_model": "base", "reload": False},
        )
        self.assertEqual(models_update.status_code, 200)

        response = client.put(
            "/api/admin/subagents",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "agents": {"general-purpose": {"model": "codex-model", "reasoning_effort": "xhigh"}},
                    "custom_agents": {},
                },
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 200)

    def test_subagents_reasoning_effort_minimal_rejected_for_codex_model(self) -> None:
        client = self._client()
        config_response = client.get("/api/admin/config", headers=self._auth_headers())
        base_model = config_response.json()["models"][0]
        codex_model = {
            "name": "codex-model",
            "use": "deerflow.models.openai_codex_provider:CodexChatModel",
            "model": "gpt-5.4",
            "supports_reasoning_effort": True,
        }
        models_update = client.put(
            "/api/admin/models",
            headers=self._auth_headers(),
            json={"models": [base_model, codex_model], "default_model": "base", "reload": False},
        )
        self.assertEqual(models_update.status_code, 200)

        # "minimal" is not a valid Codex Responses API reasoning_effort value
        # (models/factory.py:179 only accepts low/medium/high/xhigh).
        response = client.put(
            "/api/admin/subagents",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "agents": {"general-purpose": {"model": "codex-model", "reasoning_effort": "minimal"}},
                    "custom_agents": {},
                },
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("reasoning_effort", response.json()["detail"])

    def test_subagents_reasoning_effort_domain_extremes_allowed_when_model_inherited(self) -> None:
        client = self._client()

        for value in ("minimal", "xhigh"):
            response = client.put(
                "/api/admin/subagents",
                headers=self._auth_headers(),
                json={
                    "config": {
                        "enabled": True,
                        # No "model" key -> Codex vs non-Codex can't be
                        # statically resolved, so the permissive union of
                        # both domains applies. "minimal" and "xhigh" sit at
                        # opposite ends of the two domains, so both passing
                        # proves the union (not just the intersection) is used.
                        "agents": {"general-purpose": {"reasoning_effort": value}},
                        "custom_agents": {},
                    },
                    "reload": False,
                },
            )
            self.assertEqual(response.status_code, 200, f"expected '{value}' to be allowed when model is inherited")

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

    def test_memory_patch_supports_mem0_and_rejects_stale_revision(self) -> None:
        client = self._client()
        current = client.get("/api/admin/memory", headers=self._auth_headers())
        self.assertEqual(current.status_code, 200)
        revision = current.json()["revision"]

        patched = client.patch(
            "/api/admin/memory",
            headers=self._auth_headers(),
            json={
                "expected_revision": revision,
                "changes": {
                    "enabled": True,
                    "manager_class": "mem0",
                    "mode": "tool",
                    "injection_enabled": True,
                    "shutdown_flush_timeout_seconds": 12,
                    "backend_config": {
                        "api_key_env": "ADMIN_TEST_MEM0_KEY",
                        "base_url": "https://mem0.example.test",
                        "allow_insecure_http": False,
                        "default_user_id": "admin-test",
                        "top_k": 7,
                        "score_threshold": 0.25,
                        "max_injection_chars": 9000,
                        "timeout_seconds": 8,
                        "startup_policy": "best_effort",
                        "failure_policy": {"read": "fail_open", "write": "log_and_drop"},
                    },
                },
                "backend_config_mode": "replace",
                "probe": False,
                "reload": False,
            },
        )

        self.assertEqual(patched.status_code, 200, patched.text)
        payload = patched.json()
        self.assertEqual(payload["canonical_config"]["manager_class"], "mem0")
        self.assertEqual(payload["canonical_config"]["backend_config"]["top_k"], 7)
        self.assertNotEqual(payload["revision"], revision)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["memory"]["manager_class"], "mem0")
        self.assertEqual(raw["memory"]["backend_config"]["default_user_id"], "admin-test")
        self.assertNotIn("storage_path", raw["memory"])

        stale = client.patch(
            "/api/admin/memory",
            headers=self._auth_headers(),
            json={
                "expected_revision": revision,
                "changes": {"enabled": False},
                "probe": False,
                "reload": False,
            },
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "memory_revision_conflict")

    def test_memory_patch_preserves_redacted_custom_backend_secrets(self) -> None:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw["memory"] = {
            "enabled": True,
            "manager_class": "deerflow.agents.memory.backends.deermem:DeerMemManager",
            "mode": "middleware",
            "injection_enabled": True,
            "shutdown_flush_timeout_seconds": 30,
            "backend_config": {"api_key": "literal-memory-secret", "custom_option": "keep-me"},
        }
        self.config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        client = self._client()

        current = client.get("/api/admin/memory", headers=self._auth_headers())
        self.assertEqual(current.status_code, 200, current.text)
        payload = current.json()
        backend = payload["canonical_config"]["backend_config"]
        self.assertTrue(backend["api_key"]["redacted"])
        self.assertNotIn("literal-memory-secret", current.text)

        patched = client.patch(
            "/api/admin/memory",
            headers=self._auth_headers(),
            json={
                "expected_revision": payload["revision"],
                "changes": {
                    **payload["canonical_config"],
                    "mode": "tool",
                },
                "backend_config_mode": "merge",
                "probe": False,
                "reload": False,
            },
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))["memory"]
        self.assertEqual(persisted["backend_config"]["api_key"], "literal-memory-secret")
        self.assertEqual(persisted["backend_config"]["custom_option"], "keep-me")
        self.assertEqual(persisted["mode"], "tool")

    def test_memory_patch_replace_can_remove_custom_backend_fields_and_preserve_secret(self) -> None:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw["memory"] = {
            "enabled": True,
            "manager_class": "deerflow.agents.memory.backends.deermem:DeerMemManager",
            "mode": "middleware",
            "injection_enabled": True,
            "shutdown_flush_timeout_seconds": 30,
            "backend_config": {
                "api_key": "literal-memory-secret",
                "keep": "old",
                "remove_me": True,
            },
        }
        self.config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        client = self._client()
        current = client.get("/api/admin/memory", headers=self._auth_headers()).json()

        response = client.patch(
            "/api/admin/memory",
            headers=self._auth_headers(),
            json={
                "expected_revision": current["revision"],
                "changes": {
                    "backend_config": {
                        "api_key": current["canonical_config"]["backend_config"]["api_key"],
                        "keep": "new",
                    },
                },
                "backend_config_mode": "replace",
                "probe": False,
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))["memory"]["backend_config"]
        self.assertEqual(persisted["api_key"], "literal-memory-secret")
        self.assertEqual(persisted["keep"], "new")
        self.assertNotIn("remove_me", persisted)

    def test_memory_patch_without_reload_does_not_mutate_runtime_config(self) -> None:
        runtime_config = MemoryConfig(
            enabled=False,
            manager_class="deermem",
            backend_config={"max_facts": 111},
        )
        set_memory_config(runtime_config)
        client = self._client()
        current = client.get("/api/admin/memory", headers=self._auth_headers()).json()

        response = client.patch(
            "/api/admin/memory",
            headers=self._auth_headers(),
            json={
                "expected_revision": current["revision"],
                "changes": {"shutdown_flush_timeout_seconds": 17},
                "probe": False,
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))["memory"]
        self.assertEqual(persisted["shutdown_flush_timeout_seconds"], 17)
        self.assertEqual(get_memory_config(), runtime_config)

    def test_config_reads_do_not_apply_file_memory_config(self) -> None:
        runtime_config = MemoryConfig(enabled=False, manager_class="deermem")
        set_memory_config(runtime_config)
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw["memory"] = MemoryConfig(enabled=True, manager_class="deermem").model_dump()
        self.config_path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        client = self._client()
        config_response = client.get("/api/admin/config", headers=self._auth_headers())
        self.assertEqual(config_response.status_code, 200, config_response.text)
        self.assertEqual(get_memory_config(), runtime_config)

        health_response = client.get("/api/admin/config/health", headers=self._auth_headers())
        self.assertEqual(health_response.status_code, 200, health_response.text)
        self.assertTrue(health_response.json()["valid"])
        self.assertEqual(get_memory_config(), runtime_config)

    def test_memory_write_failure_leaves_runtime_and_file_unchanged_and_removes_temp_file(self) -> None:
        runtime_config = MemoryConfig(enabled=False, manager_class="deermem")
        set_memory_config(runtime_config)
        before = self.config_path.read_text(encoding="utf-8")
        client = self._client()
        current = client.get("/api/admin/memory", headers=self._auth_headers()).json()

        with patch.object(Path, "replace", side_effect=OSError("simulated replace failure")):
            response = client.patch(
                "/api/admin/memory",
                headers=self._auth_headers(),
                json={
                    "expected_revision": current["revision"],
                    "changes": {"shutdown_flush_timeout_seconds": 18},
                    "probe": False,
                    "reload": False,
                },
            )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertEqual(get_memory_config(), runtime_config)
        self.assertEqual(list(self.config_path.parent.glob("*.tmp")), [])

    def test_config_file_cas_conflict_does_not_mutate_runtime_memory(self) -> None:
        runtime_config = MemoryConfig(enabled=False, manager_class="deermem")
        set_memory_config(runtime_config)
        loaded = admin_router._load_config_data(self.config_path)
        loaded["memory"] = MemoryConfig(enabled=True, manager_class="deermem").model_dump()

        concurrent = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        concurrent["log_level"] = "DEBUG"
        self.config_path.write_text(
            yaml.safe_dump(concurrent, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        with self.assertRaises(HTTPException) as raised:
            admin_router._atomic_write_config(loaded, path=self.config_path)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(get_memory_config(), runtime_config)
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["log_level"], "DEBUG")
        self.assertNotIn("memory", persisted)

    def test_memory_reload_rollback_does_not_overwrite_newer_worker_revision(self) -> None:
        client = self._client()
        current = client.get("/api/admin/memory", headers=self._auth_headers()).json()
        reload_calls = 0

        def fail_then_sync(*, reload: bool):
            nonlocal reload_calls
            self.assertTrue(reload)
            reload_calls += 1
            if reload_calls == 1:
                concurrent = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
                concurrent["memory"]["shutdown_flush_timeout_seconds"] = 19
                self.config_path.write_text(
                    yaml.safe_dump(concurrent, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                raise RuntimeError("simulated local reload failure")
            return {"success": True}

        with patch.object(admin_router, "_reload_after_config_write", side_effect=fail_then_sync):
            response = client.patch(
                "/api/admin/memory",
                headers=self._auth_headers(),
                json={
                    "expected_revision": current["revision"],
                    "changes": {"shutdown_flush_timeout_seconds": 18},
                    "probe": False,
                    "reload": True,
                },
            )

        self.assertEqual(response.status_code, 500, response.text)
        rollback = response.json()["detail"]["rollback"]
        self.assertFalse(rollback["file_restored"])
        self.assertFalse(rollback["runtime_restored"])
        self.assertTrue(rollback["runtime_synchronized"])
        self.assertTrue(rollback["superseded_by_newer_config"])
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["memory"]["shutdown_flush_timeout_seconds"], 19)
        self.assertEqual(reload_calls, 2)

    def test_memory_validation_error_redacts_custom_backend_secret(self) -> None:
        secret = "literal-custom-memory-secret"
        candidate = {
            "enabled": True,
            "manager_class": "deermem",
            "mode": "middleware",
            "injection_enabled": True,
            "shutdown_flush_timeout_seconds": 30,
            "backend_config": {"api_key": secret},
        }
        client = self._client()

        with patch(
            "deerflow.agents.memory.validate_memory_manager_config",
            side_effect=RuntimeError(f"custom validation exposed {secret}"),
        ):
            response = client.post(
                "/api/admin/memory/validate",
                headers=self._auth_headers(),
                json={"config": candidate},
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn(secret, response.text)
        self.assertIn("[REDACTED]", response.text)

    def test_memory_custom_backend_model_name_is_not_validated_as_deermem_model(
        self,
    ) -> None:
        candidate = {
            "enabled": True,
            "manager_class": "deerflow.agents.memory.backends.deermem:DeerMemManager",
            "mode": "middleware",
            "injection_enabled": True,
            "shutdown_flush_timeout_seconds": 30,
            "backend_config": {"model_name": "external-backend-model"},
        }

        response = self._client().post(
            "/api/admin/memory/validate",
            headers=self._auth_headers(),
            json={"config": candidate},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["canonical_config"]["backend_config"]["model_name"],
            "external-backend-model",
        )

    def test_memory_probe_error_redacts_mem0_environment_secret_from_response_and_log(self) -> None:
        secret = "mem0-environment-secret-value"
        candidate = {
            "enabled": True,
            "manager_class": "mem0",
            "mode": "middleware",
            "injection_enabled": True,
            "shutdown_flush_timeout_seconds": 30,
            "backend_config": {
                "api_key_env": "ADMIN_TEST_MEM0_SECRET",
                "base_url": "https://mem0.example.test",
            },
        }
        client = self._client()

        with (
            patch.dict(os.environ, {"ADMIN_TEST_MEM0_SECRET": secret}),
            patch(
                "deerflow.agents.memory.probe_memory_manager_config",
                side_effect=RuntimeError(f"remote probe exposed {secret}"),
            ),
            self.assertLogs("app.routers.admin", level="INFO") as captured,
        ):
            response = client.post(
                "/api/admin/memory/test",
                headers=self._auth_headers(),
                json={"config": candidate},
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn(secret, response.text)
        self.assertNotIn(secret, "\n".join(captured.output))
        self.assertIn("[REDACTED]", response.text)

    def test_memory_probe_timeout_is_bounded_and_does_not_write_config(self) -> None:
        before = self.config_path.read_text(encoding="utf-8")
        candidate = {
            "enabled": True,
            "manager_class": "deermem",
            "mode": "middleware",
            "injection_enabled": True,
            "shutdown_flush_timeout_seconds": 30,
            "backend_config": {},
        }

        def slow_probe(_config: MemoryConfig) -> dict[str, Any]:
            time.sleep(0.05)
            return {"ok": True, "skipped": False}

        with (
            patch.object(admin_router, "_MEMORY_PROBE_TIMEOUT_SECONDS", 0.01),
            patch.object(admin_router, "_probe_memory_candidate", side_effect=slow_probe),
        ):
            response = self._client().post(
                "/api/admin/memory/test",
                headers=self._auth_headers(),
                json={"config": candidate},
            )

        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()["detail"]["code"], "memory_probe_timeout")
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_memory_test_reports_missing_mem0_environment_without_writing(self) -> None:
        client = self._client()
        before = self.config_path.read_text(encoding="utf-8")
        response = client.post(
            "/api/admin/memory/test",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "manager_class": "mem0",
                    "mode": "middleware",
                    "injection_enabled": True,
                    "shutdown_flush_timeout_seconds": 30,
                    "backend_config": {
                        "api_key_env": "DEERFLOW_TEST_KEY_THAT_IS_NOT_SET",
                        "base_url": "https://mem0.example.test",
                    },
                }
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "memory_probe_failed")
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_summarization_reject_enabled_without_trigger(self) -> None:
        client = self._client()
        summarization = client.put(
            "/api/admin/summarization",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "keep": {"type": "messages", "value": 20},
                },
                "reload": False,
            },
        )
        self.assertEqual(summarization.status_code, 400)
        self.assertIn("trigger", summarization.json()["detail"])

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

    def test_thread_cleanup_config_can_be_read_validated_and_hot_applied(self) -> None:
        client = self._client()
        manager = SimpleNamespace(
            thread_cleanup_service=object(),
            configure_thread_cleanup=AsyncMock(),
        )

        read = client.get("/api/admin/thread-cleanup/config", headers=self._auth_headers())
        invalid = client.put(
            "/api/admin/thread-cleanup/config",
            headers=self._auth_headers(),
            json={
                "config": {
                    "enabled": True,
                    "inactive_days": 14,
                    "run_daily_at": "03:30",
                    "timezone": "Not/A-Timezone",
                    "batch_size": 10,
                    "max_deletions_per_run": 50,
                    "protect_scheduled_threads": True,
                }
            },
        )
        with patch.object(admin_router, "get_client_manager", return_value=manager):
            updated = client.put(
                "/api/admin/thread-cleanup/config",
                headers=self._auth_headers(),
                json={
                    "config": {
                        "enabled": False,
                        "inactive_days": 45,
                        "run_daily_at": "04:15",
                        "timezone": "Asia/Shanghai",
                        "batch_size": 25,
                        "batch_interval_seconds": 2.5,
                        "max_deletions_per_run": 300,
                        "protect_scheduled_threads": False,
                        "quiet_period_minutes": 20,
                        "postpone_minutes": 12,
                        "stop_on_new_activity": False,
                    }
                },
            )

        self.assertEqual(read.status_code, 200)
        self.assertTrue(read.json()["config"]["enabled"])
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["effect"], "hot_applied")
        self.assertEqual(settings.thread_cleanup.inactive_days, 45)
        self.assertEqual(settings.thread_cleanup.batch_interval_seconds, 2.5)
        self.assertEqual(settings.thread_cleanup.quiet_period_minutes, 20)
        self.assertEqual(settings.thread_cleanup.postpone_minutes, 12)
        self.assertFalse(settings.thread_cleanup.stop_on_new_activity)
        manager.configure_thread_cleanup.assert_awaited_once()
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["api"]["thread_cleanup"]["run_daily_at"], "04:15")
        self.assertFalse(persisted["api"]["thread_cleanup"]["enabled"])

    def test_thread_cleanup_status_preview_and_run_api(self) -> None:
        client = self._client()
        service = SimpleNamespace(
            status=AsyncMock(return_value={"running_job": None, "database": {"database_bytes": 123}}),
            preview=AsyncMock(
                return_value={
                    "candidates": [{"thread_id": "old-thread"}],
                    "eligible_count": 1,
                    "estimated_reclaimable_bytes": 100,
                }
            ),
            start_run=AsyncMock(return_value={"job_id": "cleanup-test", "status": "pending"}),
        )
        manager = SimpleNamespace(thread_cleanup_service=service)

        with patch.object(admin_router, "get_client_manager", return_value=manager):
            status_response = client.get(
                "/api/admin/thread-cleanup/status",
                headers=self._auth_headers(),
            )
            preview_response = client.get(
                "/api/admin/thread-cleanup/preview?limit=17",
                headers=self._auth_headers(),
            )
            run_response = client.post(
                "/api/admin/thread-cleanup/runs",
                headers=self._auth_headers(),
                json={"dry_run": True, "limit": 17},
            )

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["database"]["database_bytes"], 123)
        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(preview_response.json()["eligible_count"], 1)
        self.assertEqual(run_response.status_code, 202)
        self.assertEqual(run_response.json()["job_id"], "cleanup-test")
        service.status.assert_awaited_once_with()
        service.preview.assert_awaited_once_with(limit=17)
        service.start_run.assert_awaited_once_with(trigger="manual", dry_run=True, limit=17)

    def test_thread_cleanup_operations_require_sqlite_service(self) -> None:
        client = self._client()
        manager = SimpleNamespace(thread_cleanup_service=None)
        settings.checkpointer_type = "memory"

        with patch.object(admin_router, "get_client_manager", return_value=manager):
            responses = [
                client.get("/api/admin/thread-cleanup/status", headers=self._auth_headers()),
                client.get("/api/admin/thread-cleanup/preview", headers=self._auth_headers()),
                client.post(
                    "/api/admin/thread-cleanup/runs",
                    headers=self._auth_headers(),
                    json={},
                ),
                client.put(
                    "/api/admin/thread-cleanup/config",
                    headers=self._auth_headers(),
                    json={"config": settings.thread_cleanup.model_dump()},
                ),
            ]

        self.assertTrue(all(response.status_code == 409 for response in responses))

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

    def test_async_client_cache_insert_is_atomic_with_reload(self) -> None:
        manager = ClientManager()
        insert_started = threading.Event()
        reload_finished = threading.Event()

        class PausingCache(dict):
            def __setitem__(self, key, value):
                insert_started.set()
                # Without the cache lock, reload finishes here and the stale
                # entry is inserted after the clear. With the lock, the wait
                # times out, insertion completes, and reload clears it next.
                reload_finished.wait(timeout=0.5)
                super().__setitem__(key, value)

        manager._async_client_map = PausingCache()

        def reload_cache() -> None:
            if not insert_started.wait(timeout=2):
                reload_finished.set()
                return
            with manager._client_cache_lock:
                manager._config_generation += 1
                manager._client_map.clear()
                manager._async_client_map.clear()
            reload_finished.set()

        reload_thread = threading.Thread(target=reload_cache, daemon=True)
        reload_thread.start()

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        with (
            patch("deerflow.client.DeerFlowClient", FakeClient),
            patch.object(manager, "_get_async_checkpointer", new=AsyncMock(return_value=object())),
        ):
            client = asyncio.run(manager.get_async_client())

        reload_thread.join(timeout=2)
        self.assertFalse(reload_thread.is_alive())
        self.assertTrue(insert_started.is_set())
        self.assertIsInstance(client, FakeClient)
        self.assertEqual(manager._config_generation, 1)
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

    def test_custom_skill_revisions_and_rollback(self) -> None:
        client = self._client()
        first = "---\nname: versioned-skill\ndescription: Version one\n---\n\n- First workflow.\n"
        second = "---\nname: versioned-skill\ndescription: Version two\n---\n\n- Second workflow.\n"

        created = client.put(
            "/api/admin/skills/custom/versioned-skill",
            headers=self._auth_headers(),
            json={"content": first, "reload": False},
        )
        updated = client.put(
            "/api/admin/skills/custom/versioned-skill",
            headers=self._auth_headers(),
            json={"content": second, "reload": False},
        )
        revisions = client.get(
            "/api/admin/skills/custom/versioned-skill/revisions",
            headers=self._auth_headers(),
        )

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["revision"], 1)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["revision"], 2)
        self.assertEqual(revisions.status_code, 200)
        self.assertEqual([item["version"] for item in revisions.json()["revisions"]], [2, 1])

        rolled_back = client.post(
            "/api/admin/skills/custom/versioned-skill/rollback/1",
            headers=self._auth_headers(),
            json={"note": "Regression found"},
        )
        current = client.get(
            "/api/admin/skills/custom/versioned-skill",
            headers=self._auth_headers(),
        )
        status = client.get("/api/admin/evolution/status", headers=self._auth_headers())

        self.assertEqual(rolled_back.status_code, 200)
        self.assertEqual(rolled_back.json()["manifest"]["version"], 3)
        self.assertEqual(rolled_back.json()["manifest"]["rollback_of"], 1)
        self.assertEqual(current.json()["content"], first)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["mode"], "review")
        self.assertGreaterEqual(status.json()["catalog_version"], 3)

    def test_evolution_proposal_review_api_publishes_only_after_approval(self) -> None:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw["skill_evolution"]["enabled"] = True
        self.config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        reset_app_config()
        client = self._client()
        content = "---\nname: reviewed-skill\ndescription: Reviewed workflow\n---\n\n- Verify first.\n"
        scanner = AsyncMock(return_value=ScanResult("allow", "Test scanner allowed candidate."))

        with patch("deerflow.skills.evolution.service.scan_skill_content", scanner):
            with admin_router._admin_app_config_context():
                proposal = asyncio.run(
                    SkillEvolutionService().create_proposal(
                        action="create",
                        name="reviewed-skill",
                        content=content,
                        reason="Reusable review workflow",
                        thread_id="thread-review",
                    )
                )

            active_file = self.skills_root / "custom" / "reviewed-skill" / "SKILL.md"
            self.assertFalse(active_file.exists())

            listed = client.get("/api/admin/evolution/proposals?status=pending_review", headers=self._auth_headers())
            detail = client.get(f"/api/admin/evolution/proposals/{proposal.id}", headers=self._auth_headers())
            archive_while_pending = client.post(
                f"/api/admin/evolution/proposals/{proposal.id}/archive",
                headers=self._auth_headers(),
            )
            approved = client.post(
                f"/api/admin/evolution/proposals/{proposal.id}/approve",
                headers=self._auth_headers(),
                json={"expected_base_sha256": None, "note": "Approved in test"},
            )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item["id"] for item in listed.json()["proposals"]], [proposal.id])
        self.assertEqual(detail.status_code, 200)
        self.assertIn("SKILL.md", detail.json()["diff"])
        self.assertEqual(archive_while_pending.status_code, 409)
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["proposal"]["status"], "published")
        self.assertEqual(active_file.read_text(encoding="utf-8"), content)

        archived = client.post(
            f"/api/admin/evolution/proposals/{proposal.id}/archive",
            headers=self._auth_headers(),
        )
        current = client.get("/api/admin/evolution/proposals", headers=self._auth_headers())
        archived_list = client.get(
            "/api/admin/evolution/proposals?archived_only=true",
            headers=self._auth_headers(),
        )
        archived_detail = client.get(
            f"/api/admin/evolution/proposals/{proposal.id}",
            headers=self._auth_headers(),
        )

        self.assertEqual(archived.status_code, 200)
        self.assertEqual(archived.json()["proposal"]["archived_by"], "admin")
        self.assertEqual(current.json()["proposals"], [])
        self.assertEqual([item["id"] for item in archived_list.json()["proposals"]], [proposal.id])
        self.assertEqual(archived_detail.json()["published_revision"], 1)
        self.assertIn("SKILL.md", archived_detail.json()["diff"])

        restored = client.post(
            f"/api/admin/evolution/proposals/{proposal.id}/restore",
            headers=self._auth_headers(),
        )
        current_after_restore = client.get("/api/admin/evolution/proposals", headers=self._auth_headers())

        self.assertEqual(restored.status_code, 200)
        self.assertIsNone(restored.json()["proposal"]["archived_at"])
        self.assertEqual([item["id"] for item in current_after_restore.json()["proposals"]], [proposal.id])
        audit = self.evolution_root.joinpath("audit.jsonl").read_text(encoding="utf-8")
        self.assertIn('"action": "proposal.archived"', audit)
        self.assertIn('"action": "proposal.restored"', audit)

    def test_evolution_proposal_batch_archive_only_archives_terminal_records(self) -> None:
        now = utc_now_iso()
        proposals = (
            SkillProposal(
                id="p_batch_published",
                status="published",
                action="edit",
                skill_name="published-skill",
                created_at=now,
                updated_at=now,
            ),
            SkillProposal(
                id="p_batch_rejected",
                status="rejected",
                action="edit",
                skill_name="rejected-skill",
                created_at=now,
                updated_at=now,
            ),
            SkillProposal(
                id="p_batch_pending",
                status="pending_review",
                action="edit",
                skill_name="pending-skill",
                created_at=now,
                updated_at=now,
            ),
            SkillProposal(
                id="p_batch_archived",
                status="failed",
                action="edit",
                skill_name="already-archived-skill",
                created_at=now,
                updated_at=now,
                archived_at=now,
                archived_by="admin",
            ),
        )
        with admin_router._admin_app_config_context():
            store = get_evolution_store()
            for proposal in proposals:
                store.save_proposal(proposal)

        response = self._client().post(
            "/api/admin/evolution/proposals/archive-batch",
            headers=self._auth_headers(),
            json={
                "proposal_ids": [
                    "p_batch_published",
                    "p_batch_rejected",
                    "p_batch_pending",
                    "p_batch_archived",
                    "p_batch_missing",
                    "p_batch_published",
                ]
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["requested_count"], 5)
        self.assertEqual(payload["archived_ids"], ["p_batch_published", "p_batch_rejected"])
        self.assertEqual(payload["already_archived_ids"], ["p_batch_archived"])
        self.assertEqual(
            {(item["proposal_id"], item["reason"]) for item in payload["skipped"]},
            {("p_batch_pending", "not_terminal"), ("p_batch_missing", "not_found")},
        )
        with admin_router._admin_app_config_context():
            store = get_evolution_store()
            self.assertIsNotNone(store.load_proposal("p_batch_published").archived_at)
            self.assertIsNotNone(store.load_proposal("p_batch_rejected").archived_at)
            self.assertIsNone(store.load_proposal("p_batch_pending").archived_at)
        audit = self.evolution_root.joinpath("audit.jsonl").read_text(encoding="utf-8")
        self.assertEqual(audit.count('"action": "proposal.archived"'), 2)

    def test_evolution_signal_and_worker_observability_api(self) -> None:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw["skill_evolution"]["enabled"] = True
        raw["skill_evolution"]["discovery"] = {"enabled": True}
        self.config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        reset_app_config()
        now = utc_now_iso()
        with admin_router._admin_app_config_context():
            store = get_evolution_store()
            store.save_signal(
                EvolutionSignal(
                    id="s_admin_test",
                    fingerprint="abc123",
                    trigger_types=["repeated_task"],
                    user_summary="Sanitized recurring task",
                    assistant_summary="Done",
                    tool_names=["web_search"],
                    tool_count=1,
                    tool_error_count=1,
                    unresolved_error_count=1,
                    tool_errors=[
                        {
                            "sequence": 1,
                            "tool_name": "web_search",
                            "message": "Error: upstream timeout",
                            "recovered": False,
                        }
                    ],
                    recurrence_count=2,
                    created_at=now,
                    updated_at=now,
                )
            )

        client = self._client()
        signals = client.get("/api/admin/evolution/signals", headers=self._auth_headers())
        detail = client.get("/api/admin/evolution/signals/s_admin_test", headers=self._auth_headers())
        status = client.get("/api/admin/evolution/status", headers=self._auth_headers())

        self.assertEqual(signals.status_code, 200)
        self.assertEqual(signals.json()["signals"][0]["id"], "s_admin_test")
        self.assertNotIn("tool_errors", signals.json()["signals"][0])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["tool_errors"][0]["message"], "Error: upstream timeout")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["signal_counts"]["pending"], 1)
        self.assertIn("worker", status.json())
        self.assertIn("probations", status.json())

        deleted = client.delete("/api/admin/evolution/signals/s_admin_test", headers=self._auth_headers())
        missing = client.get("/api/admin/evolution/signals/s_admin_test", headers=self._auth_headers())
        status_after_delete = client.get("/api/admin/evolution/status", headers=self._auth_headers())

        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["success"])
        self.assertFalse(deleted.json()["proposal_preserved"])
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(status_after_delete.json()["signal_counts"].get("pending", 0), 0)
        self.assertIn('"action": "signal.deleted"', self.evolution_root.joinpath("audit.jsonl").read_text(encoding="utf-8"))

    def test_evolution_observability_cleanup_preserves_active_state_and_history(self) -> None:
        now = utc_now_iso()
        with admin_router._admin_app_config_context():
            store = get_evolution_store()
            state = store.read_state()
            state["observations"] = {"repeat-fingerprint": {"count": 2, "last_signal_at": now}}
            store.write_state(state)
            store.set_probation("active-skill", {"status": "probation", "revision": 3})
            store.set_probation("alert-skill", {"status": "alert", "revision": 4})
            store.set_probation("graduated-skill", {"status": "graduated", "revision": 5})
            for signal in (
                EvolutionSignal(
                    id="s_cleanup_pending",
                    status="pending",
                    fingerprint="pending",
                    created_at=now,
                    updated_at=now,
                ),
                EvolutionSignal(
                    id="s_cleanup_done",
                    status="proposal_created",
                    fingerprint="done",
                    proposal_id="p_preserved",
                    created_at=now,
                    updated_at=now,
                ),
                EvolutionSignal(
                    id="s_cleanup_processing",
                    status="processing",
                    fingerprint="processing",
                    created_at=now,
                    updated_at=now,
                ),
            ):
                store.save_signal(signal)

        response = self._client().post(
            "/api/admin/evolution/observability/cleanup",
            headers=self._auth_headers(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload["deleted_signal_ids"]), {"s_cleanup_pending", "s_cleanup_done"})
        self.assertEqual(payload["skipped_signal_count"], 1)
        self.assertEqual(payload["preserved_proposal_ids"], ["p_preserved"])
        self.assertEqual(payload["deleted_probations"], ["graduated-skill"])
        self.assertTrue(payload["observations_preserved"])
        with admin_router._admin_app_config_context():
            store = get_evolution_store()
            self.assertEqual(store.load_signal("s_cleanup_processing").status, "processing")
            self.assertEqual({item.id for item in store.list_signals()}, {"s_cleanup_processing"})
            self.assertEqual(set(store.get_probations()), {"active-skill", "alert-skill"})
            self.assertIn("repeat-fingerprint", store.read_state()["observations"])
        audit = self.evolution_root.joinpath("audit.jsonl").read_text(encoding="utf-8")
        self.assertIn('"action": "signal.deleted"', audit)
        self.assertIn('"action": "probation.cleaned"', audit)

    def test_processing_evolution_signal_cannot_be_deleted(self) -> None:
        now = utc_now_iso()
        with admin_router._admin_app_config_context():
            store = get_evolution_store()
            store.save_signal(
                EvolutionSignal(
                    id="s_processing_test",
                    status="processing",
                    fingerprint="processing",
                    created_at=now,
                    updated_at=now,
                )
            )

        response = self._client().delete(
            "/api/admin/evolution/signals/s_processing_test",
            headers=self._auth_headers(),
        )

        self.assertEqual(response.status_code, 409)
        with admin_router._admin_app_config_context():
            self.assertEqual(get_evolution_store().load_signal("s_processing_test").status, "processing")

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

    def test_runtime_patch_exposes_scheduler_reliability_settings(self) -> None:
        response = self._client().patch(
            "/api/admin/runtime",
            headers=self._auth_headers(),
            json={
                "scheduler_max_concurrent_runs": 8,
                "scheduler_max_attempts": 5,
                "scheduler_retry_base_seconds": 20,
                "scheduler_claim_lease_seconds": 180,
                "scheduler_shutdown_grace_seconds": 15,
                "scheduler_run_retention_days": 60,
                "scheduler_max_runs_per_task": 2000,
                "reload": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(
            all(effect == "requires_restart" for effect in payload["effects"].values())
        )
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["api"]["scheduler_max_concurrent_runs"], 8)
        self.assertEqual(raw["api"]["scheduler_max_attempts"], 5)
        self.assertEqual(raw["api"]["scheduler_max_runs_per_task"], 2000)

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
