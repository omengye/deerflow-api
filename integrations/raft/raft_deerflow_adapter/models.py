"""Small data models shared by the adapter components."""

from __future__ import annotations

from dataclasses import dataclass


_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def _is_thread_id(value: str) -> bool:
    return len(value) == 8 and all(char in _HEX_CHARS for char in value)


@dataclass(frozen=True, slots=True)
class RaftMessage:
    """One message drained from the Raft agent inbox."""

    target: str
    message_id: str
    timestamp: str
    sender_type: str
    sender: str
    content: str

    @property
    def key(self) -> str:
        # Raft can expose the same DM through both participants' aliases, for
        # example dm:@deerflow:<thread> and dm:@human:<thread>.  The message id
        # and timestamp identify the logical inbox item independently of that
        # presentation alias, so both copies must resolve to one durable row.
        return f"{self.message_id}|{self.timestamp}"

    @property
    def reply_target(self) -> str:
        if self.target.startswith("agent-event:"):
            return self.target

        if self.target.startswith("dm:"):
            # Always address a DM reply to the sender.  Depending on which
            # cursor/wake caused a drain, Raft may label the same conversation
            # with either the local agent or remote participant's handle.
            sender_handle = self.sender.split(maxsplit=1)[0]
            if not sender_handle.startswith("@"):
                sender_handle = self.target.split(":", 2)[1]
            tail = self.target.rsplit(":", 1)[-1]
            if _is_thread_id(tail):
                return f"dm:{sender_handle}:{tail}"
            # An unthreaded DM should stay in the main DM. Creating a thread
            # here makes the response easy to miss in Raft's conversation UI.
            return f"dm:{sender_handle}"

        tail = self.target.rsplit(":", 1)[-1]
        if _is_thread_id(tail):
            return self.target
        return f"{self.target}:{self.message_id}"


@dataclass(frozen=True, slots=True)
class RaftCheckResult:
    messages: list[RaftMessage]
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class PendingMessage:
    key: str
    target: str
    reply_target: str
    message_id: str
    timestamp: str
    sender_type: str
    sender: str
    content: str
    attempts: int
    response_content: str | None
