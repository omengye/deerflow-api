import json

import pytest

from deerflow.tools import host_opencli as host_opencli_module


class _FakeProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b'{"ok":true}\n', b""

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode


def test_host_opencli_schema_exposes_arguments_without_pydantic_varargs() -> None:
    schema = host_opencli_module.host_opencli_tool.tool_call_schema.model_json_schema()

    assert "arguments" in schema["properties"]
    arguments_schema = schema["properties"]["arguments"]
    assert {"items": {"type": "string"}, "type": "array"} in arguments_schema["anyOf"]
    assert "v__args" not in schema["properties"]


@pytest.mark.asyncio
async def test_host_opencli_tool_routes_arguments_from_agent_input(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run(site: str, command: str, arguments: list[str] | None = None) -> str:
        captured.update(site=site, command=command, arguments=arguments)
        return "OK"

    monkeypatch.setattr(host_opencli_module, "_run_host_opencli", fake_run)

    result = await host_opencli_module.host_opencli_tool.ainvoke(
        {
            "description": "Search Twitter",
            "site": "twitter",
            "command": "search",
            "arguments": ["openai", "--limit", "5"],
        }
    )

    assert result == "OK"
    assert captured == {
        "site": "twitter",
        "command": "search",
        "arguments": ["openai", "--limit", "5"],
    }


@pytest.mark.asyncio
async def test_host_opencli_allows_an_entire_site(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*argv: str, **kwargs: object) -> _FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(host_opencli_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await host_opencli_module._run_host_opencli("twitter", "search", ["openai", "--limit", "5"])

    assert captured["argv"] == ("opencli", "twitter", "search", "openai", "--limit", "5", "--format", "json")
    assert json.loads(result) == {"exit_code": 0, "stdout": '{"ok":true}\n', "stderr": ""}


@pytest.mark.asyncio
async def test_host_opencli_blocks_a_site_not_in_allowed_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_if_called(*args: object, **kwargs: object) -> _FakeProcess:
        raise AssertionError("subprocess must not be started for a blocked site")

    monkeypatch.setattr(host_opencli_module.asyncio, "create_subprocess_exec", fail_if_called)

    result = await host_opencli_module._run_host_opencli("docker", "ps")

    assert result == "Error: OpenCLI site is not allowed: docker"
