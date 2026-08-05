from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from deerflow.agents.memory.storage import FileMemoryStorage


def _cross_process_mutation(memory_path: str, label: str, start) -> None:
    from deerflow.agents.memory import retrieval

    storage = FileMemoryStorage()
    path = Path(memory_path)
    storage._get_memory_file_path = lambda _agent_name=None: path  # type: ignore[method-assign]
    retrieval.rebuild_memory_index = lambda *_args, **_kwargs: False
    assert start.wait(5)

    def append_fact(data):
        data.setdefault("facts", []).append({"id": label, "content": label})
        # Keep the transaction open long enough that an implementation with
        # only per-process locks deterministically loses one update.
        time.sleep(0.15)
        return data

    assert storage.mutate(append_fact) is not None


def test_file_memory_failed_replace_preserves_source_and_removes_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_path = tmp_path / "memory.json"
    original = {"version": "1.0", "facts": [{"id": "old", "content": "old"}]}
    memory_path.write_text(json.dumps(original), encoding="utf-8")
    storage = FileMemoryStorage()
    monkeypatch.setattr(storage, "_get_memory_file_path", lambda _agent_name=None: memory_path)
    monkeypatch.setattr(
        "deerflow.agents.memory.retrieval.rebuild_memory_index",
        lambda *_args, **_kwargs: False,
    )

    def fail_replace(_self: Path, _target: Path):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    assert storage.save({"version": "1.0", "facts": []}) is False
    assert json.loads(memory_path.read_text(encoding="utf-8")) == original
    assert list(tmp_path.glob("memory.*.tmp")) == []


def test_file_memory_mutation_does_not_overwrite_corrupt_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_path = tmp_path / "memory.json"
    corrupt = '{"version": "1.0", "facts": ['
    memory_path.write_text(corrupt, encoding="utf-8")
    storage = FileMemoryStorage()
    monkeypatch.setattr(
        storage,
        "_get_memory_file_path",
        lambda _agent_name=None: memory_path,
    )

    assert storage.mutate(lambda data: {**data, "facts": []}) is None
    assert memory_path.read_text(encoding="utf-8") == corrupt
    assert list(tmp_path.glob("memory.*.tmp")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX flock production path")
def test_file_memory_mutation_is_serialized_across_processes(tmp_path: Path) -> None:
    memory_path = tmp_path / "memory.json"
    memory_path.write_text(json.dumps({"version": "1.0", "facts": []}), encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    workers = [
        context.Process(target=_cross_process_mutation, args=(str(memory_path), label, start))
        for label in ("worker-a", "worker-b")
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0

    persisted = json.loads(memory_path.read_text(encoding="utf-8"))
    assert {fact["id"] for fact in persisted["facts"]} == {"worker-a", "worker-b"}
    assert memory_path.with_name(".memory.json.lock").exists()
