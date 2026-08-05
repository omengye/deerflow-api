"""Small synchronous client for mem0's public HTTP API."""

from __future__ import annotations

import os
from typing import Any

import httpx

from .config import Mem0Config


class Mem0HttpClient:
    def __init__(
        self,
        config: Mem0Config,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(
                f"mem0 API key environment variable {config.api_key_env!r} is not set"
            )
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "application/json",
            },
        )

    @staticmethod
    def _identity(
        *, user_id: str, agent_name: str | None, thread_id: str | None
    ) -> dict[str, str]:
        identity = {"user_id": user_id}
        if agent_name:
            identity["agent_id"] = agent_name
        if thread_id:
            identity["run_id"] = thread_id
        return identity

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        response = self._client.request(
            method, path, params=params, json=payload
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("POST", path, payload=payload)

    @staticmethod
    def _results(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("results", "memories", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = value.get("results") or value.get("memories")
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
        return []

    def ping(self, *, user_id: str) -> None:
        # Use a sentinel identity so startup validation never reads user data.
        self.list_memories(user_id="__deerflow_startup_check__", limit=1)

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        agent_name: str | None,
        thread_id: str | None,
    ) -> Any:
        return self._post(
            "/v3/memories/add/",
            {
                "messages": messages,
                **self._identity(
                    user_id=user_id,
                    agent_name=agent_name,
                    thread_id=thread_id,
                ),
            },
        )

    def list_memories(
        self,
        *,
        user_id: str,
        agent_name: str | None = None,
        thread_id: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        filters = self._identity(
            user_id=user_id,
            agent_name=agent_name,
            thread_id=thread_id,
        )
        results: list[dict[str, Any]] = []
        page = 1
        page_size = min(limit, 200)
        while len(results) < limit:
            payload = self._request(
                "POST",
                "/v3/memories/",
                params={"page": page, "page_size": page_size},
                payload={"filters": filters},
            )
            results.extend(self._results(payload))
            if not isinstance(payload, dict) or not payload.get("next"):
                break
            page += 1
        return results[:limit]

    def search(
        self,
        query: str,
        *,
        user_id: str,
        agent_name: str | None = None,
        thread_id: str | None = None,
        top_k: int = 8,
        threshold: float = 0.1,
    ) -> list[dict[str, Any]]:
        payload = self._post(
            "/v3/memories/search/",
            {
                "query": query,
                "filters": self._identity(
                    user_id=user_id, agent_name=agent_name, thread_id=thread_id
                ),
                "top_k": top_k,
                "threshold": threshold,
            },
        )
        return self._results(payload)

    def clear(
        self,
        *,
        user_id: str,
        agent_name: str | None = None,
        thread_id: str | None = None,
    ) -> Any:
        return self._request(
            "DELETE",
            "/v1/memories/",
            params=self._identity(
                user_id=user_id,
                agent_name=agent_name,
                thread_id=thread_id,
            ),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
