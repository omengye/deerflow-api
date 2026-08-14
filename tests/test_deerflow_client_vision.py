from types import SimpleNamespace
from unittest.mock import patch

from deerflow.client import DeerFlowClient


def _client() -> DeerFlowClient:
    client = object.__new__(DeerFlowClient)
    client._agent = None
    client._agent_config_key = None
    client._checkpointer = object()
    client._effective_checkpointer = None
    client._model_name = None
    client._thinking_enabled = True
    client._subagent_enabled = False
    client._plan_mode = False
    client._max_concurrent_subagents = 3
    client._agent_name = None
    client._available_skills = None
    client._middlewares = []
    client._recursion_limit = 200
    return client


def _app_config(*, supports_vision: bool):
    first_model = SimpleNamespace(name="first-model", supports_vision=False)
    model = SimpleNamespace(name="vision-model", supports_vision=supports_vision)
    by_name = {candidate.name: candidate for candidate in (first_model, model)}
    return SimpleNamespace(
        models=[first_model, model],
        default_model="vision-model",
        subagents=SimpleNamespace(enabled=True),
        get_model_config=by_name.get,
        get_default_model_name=lambda: "vision-model",
    )


def test_deerflow_client_uses_default_model_name_for_vision_middleware() -> None:
    client = _client()
    config = {
        "configurable": {
            "model_name": None,
            "thinking_enabled": True,
            "subagent_enabled": False,
            "is_plan_mode": False,
            "max_concurrent_subagents": 3,
        }
    }

    with (
        patch("deerflow.client.get_app_config", return_value=_app_config(supports_vision=True)),
        patch.object(DeerFlowClient, "_get_memory_signature", return_value=None),
        patch("deerflow.client.create_chat_model", return_value=object()) as create_model,
        patch.object(DeerFlowClient, "_get_tools", return_value=[]) as get_tools,
        patch("deerflow.client._build_middlewares", return_value=[]) as build_middlewares,
        patch("deerflow.client.apply_prompt_template", return_value="system"),
        patch("deerflow.client.create_agent", return_value=object()),
    ):
        client._ensure_agent(config)

    create_model.assert_called_once()
    assert create_model.call_args.kwargs["name"] == "vision-model"
    assert get_tools.call_args.kwargs["model_name"] == "vision-model"
    assert build_middlewares.call_args.kwargs["model_name"] == "vision-model"


def test_deerflow_client_rebuilds_agent_when_supports_vision_changes() -> None:
    client = _client()
    config = {
        "configurable": {
            "model_name": None,
            "thinking_enabled": True,
            "subagent_enabled": False,
            "is_plan_mode": False,
            "max_concurrent_subagents": 3,
        }
    }

    with (
        patch(
            "deerflow.client.get_app_config",
            side_effect=[
                _app_config(supports_vision=False),
                _app_config(supports_vision=True),
            ],
        ),
        patch.object(DeerFlowClient, "_get_memory_signature", return_value=None),
        patch("deerflow.client.create_chat_model", return_value=object()),
        patch.object(DeerFlowClient, "_get_tools", return_value=[]),
        patch("deerflow.client._build_middlewares", return_value=[]),
        patch("deerflow.client.apply_prompt_template", return_value="system"),
        patch("deerflow.client.create_agent", return_value=object()) as create_agent,
    ):
        client._ensure_agent(config)
        client._ensure_agent(config)

    assert create_agent.call_count == 2


def test_deerflow_client_rebuilds_agent_when_calendar_date_changes() -> None:
    client = _client()
    config = {
        "configurable": {
            "model_name": None,
            "thinking_enabled": True,
            "subagent_enabled": False,
            "is_plan_mode": False,
            "max_concurrent_subagents": 3,
        }
    }

    with (
        patch("deerflow.client.get_current_date", side_effect=["2042-03-04, Tuesday", "2042-03-05, Wednesday"]),
        patch("deerflow.client.get_app_config", return_value=_app_config(supports_vision=False)),
        patch.object(DeerFlowClient, "_get_memory_signature", return_value=None),
        patch("deerflow.client.create_chat_model", return_value=object()),
        patch.object(DeerFlowClient, "_get_tools", return_value=[]),
        patch("deerflow.client._build_middlewares", return_value=[]),
        patch("deerflow.client.apply_prompt_template", return_value="system") as apply_prompt,
        patch("deerflow.client.create_agent", return_value=object()) as create_agent,
    ):
        client._ensure_agent(config)
        client._ensure_agent(config)

    assert create_agent.call_count == 2
    assert [call.kwargs["current_date"] for call in apply_prompt.call_args_list] == [
        "2042-03-04, Tuesday",
        "2042-03-05, Wednesday",
    ]


def test_deerflow_client_explicit_model_overrides_configured_default() -> None:
    client = _client()
    client._model_name = "first-model"
    config = {
        "configurable": {
            "model_name": "first-model",
            "thinking_enabled": True,
            "subagent_enabled": False,
            "is_plan_mode": False,
            "max_concurrent_subagents": 3,
        }
    }

    with (
        patch("deerflow.client.get_app_config", return_value=_app_config(supports_vision=True)),
        patch.object(DeerFlowClient, "_get_memory_signature", return_value=None),
        patch("deerflow.client.create_chat_model", return_value=object()) as create_model,
        patch.object(DeerFlowClient, "_get_tools", return_value=[]),
        patch("deerflow.client._build_middlewares", return_value=[]),
        patch("deerflow.client.apply_prompt_template", return_value="system"),
        patch("deerflow.client.create_agent", return_value=object()),
    ):
        client._ensure_agent(config)

    assert create_model.call_args.kwargs["name"] == "first-model"


def test_deerflow_client_adds_client_mcp_tools_without_overriding_existing() -> None:
    client = _client()
    existing = SimpleNamespace(name="deerflow_tool")
    client._additional_mcp_tools = [
        SimpleNamespace(name="deerflow_tool"),
        SimpleNamespace(name="codeg_read_file"),
    ]

    with patch("deerflow.tools.get_available_tools", return_value=[existing]):
        tools = client._get_tools(model_name=None, subagent_enabled=False)

    assert tools == [existing, client._additional_mcp_tools[1]]
