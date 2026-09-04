from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path


def value_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--profile"]:
        args = args[2:]

    inbox = Path(os.environ["FAKE_RAFT_INBOX"])
    checked = Path(os.environ["FAKE_RAFT_CHECKED"])
    ready = Path(os.environ["FAKE_RAFT_READY"])
    sent = Path(os.environ["FAKE_RAFT_SENT"])

    if args[:2] == ["message", "check"]:
        if os.getenv("FAKE_RAFT_CHECK_MODE") == "transport_error":
            sys.stderr.write(
                "Error: Agent API events transport request failed\n"
                "Code: CHECK_FAILED\n"
            )
            raise SystemExit(1)
        if ready.exists() and not checked.exists():
            checked.write_text("checked", encoding="utf-8")
            sys.stdout.write(inbox.read_text(encoding="utf-8"))
        else:
            sys.stdout.write("No new inbox messages.\n")
        return

    if args[:2] == ["message", "send"]:
        target = value_after(args, "--target")
        send_draft = "--send-draft" in args
        content = "" if send_draft else sys.stdin.read()
        draft = sent.with_suffix(".draft")
        if os.getenv("FAKE_RAFT_SEND_MODE") == "delivery_unknown":
            sys.stderr.write(
                "Error: Failed to send message\n"
                "Code: SERVER_5XX\n"
                "Draft saved: yes\n"
                "Next action: Delivery state is UNKNOWN and not retryable. "
                "Do not resend on this evidence.\n"
            )
            raise SystemExit(1)
        if os.getenv("FAKE_RAFT_SEND_MODE") == "draft_held":
            if send_draft:
                content = draft.read_text(encoding="utf-8")
                draft.unlink()
            else:
                draft.write_text(content, encoding="utf-8")
                sys.stdout.write(
                    json.dumps(
                        {
                            "ok": False,
                            "error": {
                                "code": "SEND_HELD_AS_DRAFT",
                                "effect": "draft_saved",
                            },
                        }
                    )
                )
                raise SystemExit(1)
        with sent.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"target": target, "content": content, "args": args})
                + "\n"
            )
        sys.stdout.write(json.dumps({"status": "queued", "messageId": "reply123"}))
        return

    if args[:2] == ["agent", "bridge"]:
        endpoint = value_after(args, "--wake-channel-endpoint")
        token = value_after(args, "--wake-channel-token")
        ready.write_text("ready", encoding="utf-8")
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(
                {
                    "schema": "raft-channel-wake.v1",
                    "attemptId": "attempt-1",
                    "eventId": "event-1",
                    "messageId": "deadbeef",
                }
            ).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-raft-bridge-token": token,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["ok"] is True
        assert payload["runtimeSession"]
        sys.stdout.write(json.dumps({"type": "wake_injected"}) + "\n")
        sys.stdout.flush()
        while True:
            time.sleep(0.2)

    raise SystemExit(f"unsupported fake Raft command: {args}")


if __name__ == "__main__":
    main()
