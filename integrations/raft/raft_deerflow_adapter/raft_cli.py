"""Subprocess wrapper and text parser for the Raft agent-facing CLI."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Mapping

from .models import RaftCheckResult, RaftMessage


_HEADER = re.compile(
    r"^\[target=(?P<target>\S+) msg=(?P<message_id>\S+) "
    r"time=(?P<timestamp>.+?) type=(?P<sender_type>\S+)\] "
    r"(?P<sender>.*?):(?: (?P<content>.*))?$"
)
_DRAIN_STATUS = {
    "No more new inbox messages.",
    "More messages are pending. Run `raft message check` again.",
}


def _is_thread_target(target: str) -> bool:
    """Return whether a Raft target already names an 8-char thread id."""

    tail = target.rsplit(":", 1)[-1]
    return len(tail) == 8 and all(char in "0123456789abcdefABCDEF" for char in tail)


class RaftCLIError(RuntimeError):
    pass


class RaftTransportError(RaftCLIError):
    """The CLI could not complete an Agent API transport request."""


class RaftDeliveryUnknownError(RaftCLIError):
    """The server may have committed a send, so automatic retry is unsafe."""


class RaftDraftHeldError(RaftCLIError):
    """Raft explicitly held the body as an unsent draft."""


def _delivery_is_unknown(output: str) -> bool:
    normalized = output.casefold()
    return (
        "delivery state is unknown" in normalized
        and ("not retryable" in normalized or "do not resend" in normalized)
    )


def _draft_was_held(output: str) -> bool:
    compact = re.sub(r"\s+", "", output)
    return (
        '"code":"SEND_HELD_AS_DRAFT"' in compact
        and '"effect":"draft_saved"' in compact
    )


def _transport_request_failed(output: str) -> bool:
    """Recognize the stable error emitted by the Raft Agent API client.

    Keep this deliberately narrow. ``CHECK_FAILED`` is also used for HTTP and
    authentication failures, which should not be hidden behind network retries.
    """

    return "transport request failed" in output.casefold()


@dataclass(slots=True)
class _MessageBuilder:
    target: str
    message_id: str
    timestamp: str
    sender_type: str
    sender: str
    lines: list[str]

    def build(self) -> RaftMessage:
        return RaftMessage(
            target=self.target,
            message_id=self.message_id,
            timestamp=self.timestamp,
            sender_type=self.sender_type,
            sender=self.sender,
            content="\n".join(self.lines).rstrip(),
        )


def parse_message_check(output: str) -> RaftCheckResult:
    """Parse the stable human-readable output of Raft CLI 0.0.20.

    ``raft message check`` currently has no JSON flag. Each message begins with
    a metadata header, while subsequent non-header lines belong to its body.
    """

    stripped = output.strip()
    if not stripped or stripped == "No new inbox messages.":
        return RaftCheckResult(messages=[])

    messages: list[RaftMessage] = []
    current: _MessageBuilder | None = None
    has_more = False
    for line in output.splitlines():
        if line == "More messages are pending. Run `raft message check` again.":
            has_more = True
            continue
        if line in _DRAIN_STATUS or line == "No new inbox messages.":
            continue
        match = _HEADER.match(line)
        if match:
            if current is not None:
                messages.append(current.build())
            current = _MessageBuilder(
                target=match.group("target"),
                message_id=match.group("message_id"),
                timestamp=match.group("timestamp"),
                sender_type=match.group("sender_type"),
                sender=match.group("sender"),
                lines=[match.group("content") or ""],
            )
            continue
        if current is not None:
            current.lines.append(line)
    if current is not None:
        messages.append(current.build())
    return RaftCheckResult(messages=messages, has_more=has_more)


class RaftCLI:
    def __init__(
        self,
        command: str,
        command_args: list[str],
        profile: str,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.command = command
        self.command_args = list(command_args)
        self.profile = profile
        self.env = dict(env) if env is not None else None

    def _base(self) -> list[str]:
        return [self.command, *self.command_args, "--profile", self.profile]

    async def _run(
        self, *args: str, stdin: str | None = None
    ) -> tuple[str, str]:
        process = await asyncio.create_subprocess_exec(
            *self._base(),
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        stdout, stderr = await process.communicate(
            stdin.encode("utf-8") if stdin is not None else None
        )
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            parts = [part for part in (err.strip(), out.strip()) if part]
            detail = "\n".join(parts) or f"exit code {process.returncode}"
            if _delivery_is_unknown(detail):
                raise RaftDeliveryUnknownError(
                    f"Raft CLI {' '.join(args)} has unknown delivery state: {detail}"
                )
            if _draft_was_held(detail):
                raise RaftDraftHeldError(
                    f"Raft CLI {' '.join(args)} held an unsent draft: {detail}"
                )
            if _transport_request_failed(detail):
                raise RaftTransportError(
                    f"Raft CLI {' '.join(args)} transport failed: {detail}"
                )
            raise RaftCLIError(f"Raft CLI {' '.join(args)} failed: {detail}")
        return out, err

    async def check_messages(self) -> RaftCheckResult:
        stdout, _ = await self._run("message", "check")
        return parse_message_check(stdout)

    async def send_message(self, target: str, content: str) -> dict[str, object]:
        args = ["message", "send", "--target", target]
        if not _is_thread_target(target):
            # The adapter deliberately keeps ordinary DMs at top level. Raft's
            # CLI otherwise blocks the send when its most recent read context
            # happened to be an older thread and saves an unsent draft instead.
            args.append("--target-confirmed")
        args.append("--json")
        try:
            stdout, _ = await self._run(*args, stdin=content)
        except RaftDraftHeldError:
            # SEND_HELD_AS_DRAFT explicitly guarantees that no target delivery
            # occurred. Confirm the unchanged durable body immediately instead
            # of consuming a retry or regenerating the ACP response.
            stdout, _ = await self._run(
                "message", "send", "--send-draft", "--target", target, "--json"
            )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return {"raw": stdout.strip()}
        return payload if isinstance(payload, dict) else {"result": payload}

    async def start_bridge(
        self,
        *,
        expected_agent_id: str,
        wake_endpoint: str,
        wake_token: str,
        adapter_instance: str = "deerflow-acp",
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *self._base(),
            "agent",
            "bridge",
            "--json",
            "--expected-agent",
            expected_agent_id,
            "--wake-adapter",
            "wake-channel",
            "--wake-channel-endpoint",
            wake_endpoint,
            "--wake-channel-token",
            wake_token,
            "--adapter-instance",
            adapter_instance,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
