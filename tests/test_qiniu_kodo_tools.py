import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.community.qiniu_kodo import tools as qiniu_tools
from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.tool_config import ToolConfig


class FakeInfo:
    status_code = 200
    req_id = "req-1"

    def ok(self) -> bool:
        return True


class FakeAuth:
    upload_tokens: list[dict] = []
    private_urls: list[dict] = []

    def __init__(self, access_key: str, secret_key: str) -> None:
        self.access_key = access_key
        self.secret_key = secret_key

    def upload_token(self, bucket: str, key: str, expires: int, policy: dict | None = None) -> str:
        self.upload_tokens.append(
            {
                "bucket": bucket,
                "key": key,
                "expires": expires,
                "policy": policy,
            }
        )
        return "upload-token"

    def private_download_url(self, url: str, expires: int = 3600) -> str:
        self.private_urls.append({"url": url, "expires": expires})
        return f"{url}?signed={expires}"


class FakeBucketManager:
    list_calls: list[dict] = []
    stat_calls: list[dict] = []
    delete_calls: list[dict] = []

    def __init__(self, auth: FakeAuth) -> None:
        self.auth = auth

    def list(self, bucket: str, prefix: str = "", marker: str | None = None, limit: int = 100):
        self.list_calls.append(
            {
                "bucket": bucket,
                "prefix": prefix,
                "marker": marker,
                "limit": limit,
            }
        )
        return {"items": [{"key": f"{prefix}report.txt", "fsize": 12}], "marker": "next"}, False, FakeInfo()

    def stat(self, bucket: str, key: str):
        self.stat_calls.append({"bucket": bucket, "key": key})
        return {"fsize": 12, "mimeType": "text/plain"}, FakeInfo()

    def delete(self, bucket: str, key: str):
        self.delete_calls.append({"bucket": bucket, "key": key})
        return {}, FakeInfo()


PUT_FILE_CALLS: list[dict] = []


def fake_put_file(token: str, key: str, local_path: str, **kwargs):
    PUT_FILE_CALLS.append(
        {
            "token": token,
            "key": key,
            "local_path": local_path,
            "kwargs": kwargs,
        }
    )
    return {"key": key, "hash": "hash-1"}, FakeInfo()


@pytest.fixture(autouse=True)
def clean_config():
    reset_app_config()
    FakeAuth.upload_tokens.clear()
    FakeAuth.private_urls.clear()
    FakeBucketManager.list_calls.clear()
    FakeBucketManager.stat_calls.clear()
    FakeBucketManager.delete_calls.clear()
    PUT_FILE_CALLS.clear()
    yield
    reset_app_config()


def _runtime(tmp_path: Path):
    user_data = tmp_path / "threads" / "thread-1" / "user-data"
    workspace = user_data / "workspace"
    uploads = user_data / "uploads"
    outputs = user_data / "outputs"
    for directory in (workspace, uploads, outputs):
        directory.mkdir(parents=True)

    return SimpleNamespace(
        context={"thread_id": "thread-1"},
        config={"configurable": {"thread_id": "thread-1"}},
        state={
            "thread_data": {
                "workspace_path": str(workspace),
                "uploads_path": str(uploads),
                "outputs_path": str(outputs),
            }
        },
        workspace=workspace,
        uploads=uploads,
        outputs=outputs,
    )


def _set_config(*tools: ToolConfig) -> None:
    set_app_config(
        AppConfig(
            sandbox=SandboxConfig(use="test"),
            tool_groups=[],
            tools=list(tools),
            models=[],
        )
    )


def _fake_sdk() -> qiniu_tools._QiniuSdk:
    return qiniu_tools._QiniuSdk(
        Auth=FakeAuth,
        BucketManager=FakeBucketManager,
        put_file=fake_put_file,
    )


def test_qiniu_upload_file_uses_thread_prefix_and_upload_token_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    file_path = runtime.outputs / "report.txt"
    file_path.write_text("hello", encoding="utf-8")
    _set_config(
        ToolConfig(
            name="qiniu_upload_file",
            group="object_storage",
            use="deerflow.community.qiniu_kodo.tools:qiniu_upload_file_tool",
            access_key="ak",
            secret_key="sk",
            bucket="bucket-a",
            domain="https://cdn.example.com",
            key_prefix="deerflow/{thread_id}/",
        )
    )
    monkeypatch.setattr(qiniu_tools, "_load_qiniu_sdk", _fake_sdk)

    raw = qiniu_tools.qiniu_upload_file_tool.func(runtime, "/mnt/user-data/outputs/report.txt")
    payload = json.loads(raw)

    assert payload["bucket"] == "bucket-a"
    assert payload["key"] == "deerflow/thread-1/outputs/report.txt"
    assert payload["hash"] == "hash-1"
    assert payload["url"] == "https://cdn.example.com/deerflow/thread-1/outputs/report.txt"
    assert FakeAuth.upload_tokens == [
        {
            "bucket": "bucket-a",
            "key": "deerflow/thread-1/outputs/report.txt",
            "expires": 3600,
            "policy": {"insertOnly": 1},
        }
    ]
    assert PUT_FILE_CALLS[0]["local_path"] == str(file_path)
    assert PUT_FILE_CALLS[0]["kwargs"]["mime_type"] == "text/plain"


def test_qiniu_list_objects_reuses_shared_config_from_another_qiniu_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    _set_config(
        ToolConfig(
            name="qiniu_upload_file",
            group="object_storage",
            use="deerflow.community.qiniu_kodo.tools:qiniu_upload_file_tool",
            access_key="ak",
            secret_key="sk",
            bucket="bucket-a",
            key_prefix="deerflow/{thread_id}/",
        ),
        ToolConfig(
            name="qiniu_list_objects",
            group="object_storage",
            use="deerflow.community.qiniu_kodo.tools:qiniu_list_objects_tool",
        ),
    )
    monkeypatch.setattr(qiniu_tools, "_load_qiniu_sdk", _fake_sdk)

    raw = qiniu_tools.qiniu_list_objects_tool.func(runtime, prefix="reports", limit=5000, marker="old")
    payload = json.loads(raw)

    assert payload["prefix"] == "deerflow/thread-1/reports"
    assert payload["count"] == 1
    assert payload["marker"] == "next"
    assert FakeBucketManager.list_calls == [
        {
            "bucket": "bucket-a",
            "prefix": "deerflow/thread-1/reports",
            "marker": "old",
            "limit": 1000,
        }
    ]


def test_qiniu_get_download_url_signs_private_bucket_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    _set_config(
        ToolConfig(
            name="qiniu_get_download_url",
            group="object_storage",
            use="deerflow.community.qiniu_kodo.tools:qiniu_get_download_url_tool",
            access_key="ak",
            secret_key="sk",
            bucket="bucket-a",
            domain="cdn.example.com",
            key_prefix="prefix/{thread_id}/",
            private_bucket="true",
        )
    )
    monkeypatch.setattr(qiniu_tools, "_load_qiniu_sdk", _fake_sdk)

    raw = qiniu_tools.qiniu_get_download_url_tool.func(runtime, key="a b.txt", expires=99)
    payload = json.loads(raw)

    assert payload["key"] == "prefix/thread-1/a b.txt"
    assert payload["private_bucket"] is True
    assert payload["url"] == "https://cdn.example.com/prefix/thread-1/a%20b.txt?signed=99"
    assert FakeAuth.private_urls == [
        {
            "url": "https://cdn.example.com/prefix/thread-1/a%20b.txt",
            "expires": 99,
        }
    ]


def test_qiniu_upload_rejects_paths_outside_thread_user_data(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _set_config(
        ToolConfig(
            name="qiniu_upload_file",
            group="object_storage",
            use="deerflow.community.qiniu_kodo.tools:qiniu_upload_file_tool",
            access_key="ak",
            secret_key="sk",
            bucket="bucket-a",
        )
    )

    raw = qiniu_tools.qiniu_upload_file_tool.func(runtime, "/etc/passwd")
    payload = json.loads(raw)

    assert "error" in payload
    assert "Only files under /mnt/user-data/" in payload["error"]
