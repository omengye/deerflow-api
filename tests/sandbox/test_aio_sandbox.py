from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deerflow.sandbox.aio import AioSandbox, AioSandboxProvider
from deerflow.sandbox.provider_paths import (
    AIO_SANDBOX_PROVIDER_PATH,
    normalize_sandbox_provider_path,
)


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    import subprocess

    return subprocess.CompletedProcess(args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_provider_path_aliases_normalize_aio() -> None:
    assert normalize_sandbox_provider_path("aio") == AIO_SANDBOX_PROVIDER_PATH
    assert normalize_sandbox_provider_path("docker") == AIO_SANDBOX_PROVIDER_PATH
    assert normalize_sandbox_provider_path("docker-sandbox") == AIO_SANDBOX_PROVIDER_PATH


def test_aio_sandbox_execute_uses_docker_exec() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _completed(stdout="ok\n")

    sandbox = AioSandbox("aio-test", "deer-flow-sandbox-aio-test")
    with patch("subprocess.run", side_effect=fake_run):
        assert sandbox.execute_command("echo ok") == "ok\n"

    assert calls == [["docker", "exec", "-i", "deer-flow-sandbox-aio-test", "/bin/bash", "-lc", "echo ok"]]


def test_aio_provider_mounts_thread_data_and_skills(tmp_path, monkeypatch) -> None:
    base_dir = tmp_path / "state"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setenv("DEER_FLOW_HOME", str(base_dir))
    import deerflow.config.paths as paths_mod

    paths_mod._paths = None

    config = SimpleNamespace(
        sandbox=SimpleNamespace(
            image="sandbox-image:test",
            replicas=2,
            container_prefix="df-test",
            idle_timeout=600,
            environment={"TOKEN": "$TOKEN_VALUE", "STATIC": "x"},
            mounts=[],
            security_opt=["seccomp:unconfined"],
        ),
        skills=SimpleNamespace(
            get_skills_path=lambda: skills_dir,
            container_path="/mnt/skills",
        ),
    )
    monkeypatch.setenv("TOKEN_VALUE", "secret")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "version", "--format"]:
            return _completed(stdout="25.0.0")
        return _completed(stdout="container-id")

    with patch("deerflow.config.get_app_config", return_value=config):
        with patch("subprocess.run", side_effect=fake_run):
            provider = AioSandboxProvider()
            sandbox_id = provider.acquire("thread_1")

    assert sandbox_id == "aio-thread_1"
    run_cmd = next(cmd for cmd in calls if cmd[:2] == ["docker", "run"])
    assert "--name" in run_cmd
    assert "df-test-aio-thread_1" in run_cmd
    assert f"{base_dir / 'threads' / 'thread_1' / 'user-data'}:/mnt/user-data:rw" in run_cmd
    assert f"{base_dir / 'threads' / 'thread_1' / 'acp-workspace'}:/mnt/acp-workspace:rw" in run_cmd
    assert f"{skills_dir}:/mnt/skills:ro" in run_cmd
    assert "--security-opt" in run_cmd
    assert "seccomp:unconfined" in run_cmd
    assert "TOKEN=secret" in run_cmd
    assert "STATIC=x" in run_cmd
    assert "--user" not in run_cmd


def test_aio_provider_auto_user_applies_only_to_exec(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("os.getuid", lambda: 1234, raising=False)
    monkeypatch.setattr("os.getgid", lambda: 5678, raising=False)
    import deerflow.config.paths as paths_mod

    paths_mod._paths = None
    config = SimpleNamespace(
        sandbox=SimpleNamespace(
            image="sandbox-image:test",
            replicas=1,
            container_prefix="df-test",
            idle_timeout=600,
            environment={},
            mounts=[],
            security_opt=[],
            container_user="auto",
        ),
        skills=SimpleNamespace(
            get_skills_path=lambda: Path("missing"),
            container_path="/mnt/skills",
        ),
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "version", "--format"]:
            return _completed(stdout="25.0.0")
        if cmd[:3] == ["docker", "exec", "-i"]:
            return _completed(stdout="ok\n")
        return _completed(stdout="container-id")

    with patch("deerflow.config.get_app_config", return_value=config):
        with patch("subprocess.run", side_effect=fake_run):
            provider = AioSandboxProvider()
            sandbox_id = provider.acquire("thread_1")
            sandbox = provider.get(sandbox_id)
            assert sandbox is not None
            assert sandbox.execute_command("echo ok") == "ok\n"

    run_cmd = next(cmd for cmd in calls if cmd[:2] == ["docker", "run"])
    exec_cmd = next(cmd for cmd in calls if cmd[:3] == ["docker", "exec", "-i"])
    assert "--user" not in run_cmd
    assert exec_cmd[:6] == ["docker", "exec", "-i", "-u", "1234:5678", "df-test-aio-thread_1"]


def test_aio_provider_rejects_unsafe_thread_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path / "state"))
    import deerflow.config.paths as paths_mod

    paths_mod._paths = None
    config = SimpleNamespace(
        sandbox=SimpleNamespace(
            image="sandbox-image:test",
            replicas=1,
            container_prefix="df-test",
            idle_timeout=600,
            environment={},
            mounts=[],
            security_opt=[],
        ),
        skills=SimpleNamespace(
            get_skills_path=lambda: Path("missing"),
            container_path="/mnt/skills",
        ),
    )

    def fake_run(cmd, **kwargs):
        return _completed(stdout="25.0.0")

    with patch("deerflow.config.get_app_config", return_value=config):
        with patch("subprocess.run", side_effect=fake_run):
            provider = AioSandboxProvider()
            with pytest.raises(ValueError):
                provider.acquire("../escape")
