"""Mode-safe access to materialized LangGraph checkpoint state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from deerflow.config.checkpointer_config import CheckpointChannelMode
from deerflow.runtime.checkpoint_mode import (
    aensure_checkpoint_mode_compatible,
    ensure_checkpoint_mode_compatible,
    inject_checkpoint_mode,
    raise_if_snapshot_incompatible,
)


def _finish_state_mutation(_state: dict[str, Any]) -> dict[str, Any]:
    return {}


def build_state_mutation_graph(
    as_node: str,
    mode: CheckpointChannelMode,
    state_schema: Any,
) -> Any:
    """Compile a one-node graph that applies state writes and then stops."""
    if not as_node:
        raise ValueError("as_node is required for checkpoint state mutation")
    from langgraph.graph import StateGraph

    builder = StateGraph(state_schema)
    builder.add_node(as_node, cast(Any, _finish_state_mutation))
    builder.set_entry_point(as_node)
    builder.set_finish_point(as_node)
    return builder.compile()


def graph_state_schema(graph: Any) -> Any | None:
    schemas = getattr(getattr(graph, "builder", None), "schemas", None)
    return next(iter(schemas)) if schemas else None


def graph_writable_channels(graph: Any) -> frozenset[str] | None:
    channels = getattr(graph, "channels", None)
    if not channels:
        return None
    return frozenset(
        name
        for name in channels
        if not name.startswith("__") and not name.startswith("branch:")
    )


def graph_reducer_channels(graph: Any) -> frozenset[str] | None:
    from langgraph.channels import BinaryOperatorAggregate, DeltaChannel

    channels = getattr(graph, "channels", None)
    if channels is None:
        return None
    return frozenset(
        name
        for name, channel in channels.items()
        if isinstance(channel, (BinaryOperatorAggregate, DeltaChannel))
    )


@dataclass
class CheckpointStateAccessor:
    """Bind a compiled graph to its saver and frozen channel mode.

    Delta channels cannot be read correctly from raw ``channel_values``.  This
    accessor is the common path for state materialization and mode checks.
    """

    graph: Any
    checkpointer: Any
    mode: CheckpointChannelMode

    @classmethod
    def bind(
        cls,
        graph: Any,
        checkpointer: Any,
        *,
        store: Any | None = None,
        mode: CheckpointChannelMode = "full",
    ) -> CheckpointStateAccessor:
        graph.checkpointer = checkpointer
        if store is not None:
            graph.store = store
        return cls(graph=graph, checkpointer=checkpointer, mode=mode)

    def _prepare(self, config: dict[str, Any]) -> dict[str, Any]:
        prepared = {
            **config,
            "configurable": dict(config.get("configurable", {})),
            "metadata": dict(config.get("metadata", {})),
        }
        inject_checkpoint_mode(prepared, self.mode)
        return prepared

    def get(self, config: dict[str, Any]) -> Any:
        snapshot = self.graph.get_state(self._prepare(config))
        raise_if_snapshot_incompatible(snapshot, self.mode)
        return snapshot

    async def aget(self, config: dict[str, Any]) -> Any:
        snapshot = await self.graph.aget_state(self._prepare(config))
        raise_if_snapshot_incompatible(snapshot, self.mode)
        return snapshot

    def history(
        self,
        config: dict[str, Any],
        *,
        limit: int | None = None,
    ) -> list[Any]:
        if limit is not None and limit <= 0:
            return []
        snapshots: list[Any] = []
        for snapshot in self.graph.get_state_history(self._prepare(config), limit=limit):
            raise_if_snapshot_incompatible(snapshot, self.mode)
            snapshots.append(snapshot)
            if limit is not None and len(snapshots) >= limit:
                break
        return snapshots

    async def ahistory(
        self,
        config: dict[str, Any],
        *,
        limit: int | None = None,
    ) -> list[Any]:
        if limit is not None and limit <= 0:
            return []
        snapshots: list[Any] = []
        async for snapshot in self.graph.aget_state_history(
            self._prepare(config), limit=limit
        ):
            raise_if_snapshot_incompatible(snapshot, self.mode)
            snapshots.append(snapshot)
            if limit is not None and len(snapshots) >= limit:
                break
        return snapshots

    def update(
        self,
        config: dict[str, Any],
        values: dict[str, Any],
        *,
        as_node: str | None = None,
    ) -> dict[str, Any]:
        prepared = self._prepare(config)
        ensure_checkpoint_mode_compatible(self.checkpointer, prepared, self.mode)
        return self.graph.update_state(prepared, values, as_node=as_node)

    async def aupdate(
        self,
        config: dict[str, Any],
        values: dict[str, Any],
        *,
        as_node: str | None = None,
    ) -> dict[str, Any]:
        prepared = self._prepare(config)
        await aensure_checkpoint_mode_compatible(
            self.checkpointer, prepared, self.mode
        )
        return await self.graph.aupdate_state(prepared, values, as_node=as_node)
