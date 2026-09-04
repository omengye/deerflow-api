"""SQLite-backed delivery and ACP session state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import PendingMessage, RaftMessage


class AdapterState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: sqlite3.Connection | None = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                conversation_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS inbox_messages (
                message_key TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                reply_target TEXT NOT NULL,
                message_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                sender_type TEXT NOT NULL,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                response_content TEXT,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            """
        )
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(inbox_messages)")
        }
        if "response_content" not in columns:
            self._connection.execute(
                "ALTER TABLE inbox_messages ADD COLUMN response_content TEXT"
            )
        # Older builds retried a send even when Raft reported that it may
        # already have committed. Quarantine those rows during upgrade so a
        # restart cannot create duplicate replies.
        self._connection.execute(
            """
            UPDATE inbox_messages
            SET status = 'delivery_unknown'
            WHERE status = 'pending'
              AND lower(COALESCE(last_error, '')) LIKE '%delivery state is unknown%'
              AND (
                    lower(COALESCE(last_error, '')) LIKE '%not retryable%'
                 OR lower(COALESCE(last_error, '')) LIKE '%do not resend%'
              )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("adapter state is closed")
        return self._connection

    def enqueue(self, messages: list[RaftMessage]) -> int:
        inserted = 0
        for message in messages:
            # Older adapter versions included the Raft target alias in the
            # primary key.  Check the stable message identity as well so an
            # existing database cannot replay an already-seen DM after upgrade.
            existing = self._conn().execute(
                """
                SELECT 1
                FROM inbox_messages
                WHERE message_id = ? AND timestamp = ?
                LIMIT 1
                """,
                (message.message_id, message.timestamp),
            ).fetchone()
            if existing is not None:
                continue
            cursor = self._conn().execute(
                """
                INSERT OR IGNORE INTO inbox_messages (
                    message_key, target, reply_target, message_id, timestamp,
                    sender_type, sender, content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.key,
                    message.target,
                    message.reply_target,
                    message.message_id,
                    message.timestamp,
                    message.sender_type,
                    message.sender,
                    message.content,
                ),
            )
            inserted += max(cursor.rowcount, 0)
        self._conn().commit()
        return inserted

    def pending(self, limit: int = 100) -> list[PendingMessage]:
        rows = self._conn().execute(
            """
            SELECT message_key, target, reply_target, message_id, timestamp,
                   sender_type, sender, content, attempts, response_content
            FROM inbox_messages
            WHERE status = 'pending'
            ORDER BY received_at, message_key
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            PendingMessage(
                key=row["message_key"],
                target=row["target"],
                reply_target=row["reply_target"],
                message_id=row["message_id"],
                timestamp=row["timestamp"],
                sender_type=row["sender_type"],
                sender=row["sender"],
                content=row["content"],
                attempts=row["attempts"],
                response_content=row["response_content"],
            )
            for row in rows
        ]

    def mark_done(self, key: str) -> None:
        self._conn().execute(
            """
            UPDATE inbox_messages
            SET status = 'done', completed_at = CURRENT_TIMESTAMP,
                last_error = NULL, response_content = NULL
            WHERE message_key = ?
            """,
            (key,),
        )
        self._conn().commit()

    def save_response(self, key: str, response_content: str) -> None:
        self._conn().execute(
            """
            UPDATE inbox_messages
            SET response_content = ?
            WHERE message_key = ? AND status = 'pending'
            """,
            (response_content, key),
        )
        self._conn().commit()

    def mark_failed(self, key: str, error: str, *, max_attempts: int = 5) -> bool:
        self._conn().execute(
            """
            UPDATE inbox_messages
            SET attempts = attempts + 1,
                last_error = ?,
                status = CASE
                    WHEN attempts + 1 >= ? THEN 'failed'
                    ELSE status
                END
            WHERE message_key = ? AND status = 'pending'
            """,
            (error[:2000], max_attempts, key),
        )
        self._conn().commit()
        row = self._conn().execute(
            "SELECT status FROM inbox_messages WHERE message_key = ?", (key,)
        ).fetchone()
        return row is not None and row["status"] == "failed"

    def mark_delivery_unknown(self, key: str, error: str) -> None:
        self._conn().execute(
            """
            UPDATE inbox_messages
            SET status = 'delivery_unknown', attempts = attempts + 1, last_error = ?
            WHERE message_key = ?
            """,
            (error[:8000], key),
        )
        self._conn().commit()

    def get_session(self, conversation_key: str, workspace: Path) -> str | None:
        row = self._conn().execute(
            "SELECT session_id, workspace FROM sessions WHERE conversation_key = ?",
            (conversation_key,),
        ).fetchone()
        if row is None or Path(row["workspace"]) != workspace:
            return None
        return str(row["session_id"])

    def put_session(
        self, conversation_key: str, session_id: str, workspace: Path
    ) -> None:
        self._conn().execute(
            """
            INSERT INTO sessions (conversation_key, session_id, workspace)
            VALUES (?, ?, ?)
            ON CONFLICT(conversation_key) DO UPDATE SET
                session_id = excluded.session_id,
                workspace = excluded.workspace,
                updated_at = CURRENT_TIMESTAMP
            """,
            (conversation_key, session_id, str(workspace)),
        )
        self._conn().commit()
