import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deerflow.client import DeerFlowClient
from deerflow.config.extensions_config import reset_extensions_config


class _EvolutionStore:
    def bump_catalog(self, **_kwargs) -> int:
        return 1


def test_concurrent_skill_and_mcp_updates_preserve_unrelated_fields() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        config_path = Path(tmp_dir) / "extensions_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "old": {
                            "enabled": True,
                            "type": "stdio",
                            "command": "npx",
                            "args": ["old-server"],
                        }
                    },
                    "skills": {"writer": {"enabled": True, "customSkillField": "keep"}},
                    "middlewares": ["example.middleware:KeepMe"],
                    "customTopLevel": {"keep": True},
                }
            ),
            encoding="utf-8",
        )
        fake_skill = SimpleNamespace(
            name="writer",
            description="Writer",
            license=None,
            category="custom",
            enabled=False,
        )
        client = DeerFlowClient.__new__(DeerFlowClient)
        client._agent = object()
        client._agent_config_key = ("cached",)
        mcp_servers = {
            "web": {
                "enabled": True,
                "type": "stdio",
                "command": "npx",
                "args": ["web-server"],
                "customServerField": "keep",
            }
        }

        with (
            patch.dict(os.environ, {"DEER_FLOW_EXTENSIONS_CONFIG_PATH": str(config_path)}),
            patch("deerflow.skills.loader.load_skills", return_value=[fake_skill]),
            patch("deerflow.skills.evolution.get_evolution_store", return_value=_EvolutionStore()),
            patch("deerflow.agents.lead_agent.prompt.clear_skills_system_prompt_cache"),
        ):
            reset_extensions_config()
            with ThreadPoolExecutor(max_workers=2) as executor:
                for _ in range(20):
                    skill_future = executor.submit(client.update_skill, "writer", enabled=False)
                    mcp_future = executor.submit(client.update_mcp_config, mcp_servers)
                    skill_future.result(timeout=10)
                    mcp_future.result(timeout=10)
            reset_extensions_config()

        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert data["mcpServers"] == mcp_servers
        assert data["skills"]["writer"] == {"enabled": False, "customSkillField": "keep"}
        assert data["middlewares"] == ["example.middleware:KeepMe"]
        assert data["customTopLevel"] == {"keep": True}
