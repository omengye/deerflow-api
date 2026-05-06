import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.dependencies import ClientManager
from app.middleware import ApiKeyAuthMiddleware
from deerflow.runtime import ConflictError, RunStatus


class ProductionControlsTests(unittest.IsolatedAsyncioTestCase):
    def _auth_test_client(self) -> TestClient:
        app = FastAPI()
        app.add_middleware(ApiKeyAuthMiddleware)

        @app.get("/api/chat")
        async def protected():
            return {"ok": True}

        @app.get("/health")
        async def public():
            return {"status": "ok"}

        return TestClient(app)

    async def test_api_auth_rejects_missing_bearer_token(self) -> None:
        original_enabled = settings.auth_enabled
        original_keys = list(settings.api_keys)
        settings.auth_enabled = True
        settings.api_keys = ["secret"]
        try:
            client = self._auth_test_client()
            response = client.get("/api/chat")
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"detail": "Unauthorized"})
        finally:
            settings.auth_enabled = original_enabled
            settings.api_keys = original_keys

    async def test_api_auth_allows_valid_bearer_token_and_public_health(self) -> None:
        original_enabled = settings.auth_enabled
        original_keys = list(settings.api_keys)
        settings.auth_enabled = True
        settings.api_keys = ["secret"]
        try:
            client = self._auth_test_client()
            protected = client.get("/api/chat", headers={"Authorization": "Bearer secret"})
            public = client.get("/health")
            self.assertEqual(protected.status_code, 200)
            self.assertEqual(public.status_code, 200)
        finally:
            settings.auth_enabled = original_enabled
            settings.api_keys = original_keys

    async def test_run_manager_rejects_second_inflight_run_on_same_thread(self) -> None:
        manager = ClientManager()
        await manager.run_manager.create_or_reject("thread-1", multitask_strategy="reject")

        with self.assertRaises(ConflictError):
            await manager.run_manager.create_or_reject("thread-1", multitask_strategy="reject")

    async def test_cancel_run_marks_inflight_run_interrupted(self) -> None:
        manager = ClientManager()
        record = await manager.run_manager.create_or_reject("thread-1")
        await manager.run_manager.set_status(record.run_id, RunStatus.running)

        cancelled = await manager.cancel_run(record.run_id)

        self.assertTrue(cancelled)
        self.assertEqual(record.status, RunStatus.interrupted)

    async def test_sqlite_checkpointer_failure_does_not_fallback_by_default(self) -> None:
        original_type = settings.checkpointer_type
        original_path = settings.checkpointer_path
        original_fallback = settings.allow_memory_fallback
        settings.checkpointer_type = "sqlite"
        settings.checkpointer_path = "/tmp/deerflow-api-test/checkpoints.db"
        settings.allow_memory_fallback = False

        class _Conn:
            def close(self) -> None:
                pass

        try:
            manager = ClientManager()
            with patch("app.dependencies.sqlite3.connect", return_value=_Conn()):
                with patch("langgraph.checkpoint.sqlite.SqliteSaver", side_effect=RuntimeError("sqlite unavailable")):
                    with self.assertRaises(RuntimeError):
                        manager._get_checkpointer()
        finally:
            settings.checkpointer_type = original_type
            settings.checkpointer_path = original_path
            settings.allow_memory_fallback = original_fallback


if __name__ == "__main__":
    unittest.main()
