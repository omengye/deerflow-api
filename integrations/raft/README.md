# Raft DeerFlow Adapter

This sidecar connects a Raft External Agent to DeerFlow Portable without
changing DeerFlow's ACP implementation.

```text
Raft Server
    <-> raft agent bridge / raft message CLI
    <-> raft-deerflow-adapter
    <-> ACP v1 JSON-RPC over stdio
    <-> deerflow-acp.exe
    <-> persistent deerflow-acpd
```

The adapter was implemented against `@botiverse/raft` 0.0.20. That release
does not expose JSON output for `raft message check`, so the adapter parses its
documented metadata header. `raft message send --json` is used for replies.

## Prerequisites

1. Extract and configure DeerFlow Portable. Start it once from
   `deerflow-config.exe` so its model configuration exists.
2. Install Node.js 20 or newer and the Raft CLI:

   ```powershell
   npm install -g @botiverse/raft@latest
   ```

3. In Raft, create an External Agent and copy its server URL and Agent id.
4. Mint the local profile and approve the device login in a browser:

   ```powershell
   raft agent login --server <SERVER_URL> --agent <AGENT_ID> --profile-slug deerflow
   ```

## Configure

Copy `adapter.example.toml` to `adapter.toml`, then set:

- `raft.profile` to the profile slug used during login;
- `raft.agent_id` to the External Agent id shown by Raft;
- `deerflow.command` to the absolute `deerflow-acp.exe` path;
- `deerflow.workspace` to an existing local working directory.

The workspace is passed as ACP `session/new.cwd`. Raft files are not
automatically synchronized in this first implementation.

## Run

From this directory, reuse the parent project's environment:

```powershell
..\..\.venv\Scripts\python.exe -m raft_deerflow_adapter --config .\adapter.toml
```

Or install the adapter in its own environment:

```powershell
uv sync
uv run raft-deerflow-adapter --config .\adapter.toml
```

On Windows, after `adapter.toml` is configured, you can also double-click
`start.bat` or run it from a terminal. The script prefers the adapter's local
`.venv` and falls back to `uv run` only when that environment is absent:

```powershell
.\start.bat
```

Stop the Windows adapter process and its Raft bridge child processes with:

```powershell
.\stop.bat
```

The running adapter records its verified process id in `data/adapter.pid`, so
the stop script does not depend on elevated process-command-line inspection.

To verify which adapter process would be stopped without changing anything:

```powershell
.\stop.bat -DryRun
```

Startup performs these actions:

1. launches `deerflow-acp.exe` and negotiates ACP v1;
2. opens a loopback `/wake` endpoint;
3. launches `raft --profile <slug> agent bridge` with the wake endpoint;
4. drains `raft message check` after every wake and periodically as fallback;
5. maps each Raft conversation/thread to a persistent ACP session;
6. sends DeerFlow's final text back with `raft message send`.

Transient Agent API transport failures while checking the inbox are retried
immediately with bounded exponential backoff. Configure the attempt count and
delay using `adapter.inbox_transport_retry_attempts`,
`adapter.inbox_transport_retry_base_seconds`, and
`adapter.inbox_transport_retry_max_seconds`. Authentication, HTTP response,
protocol, and message-send errors are deliberately not included in this retry.

The adapter also exposes Raft's `GET /activity/drain?max=N` companion endpoint.
It reports session start/end, prompt processing, ACP thinking, reply start, tool
start/end, completion, and failures. Activity is held in a bounded in-memory
queue (500 events, oldest dropped first) and is loss-tolerant telemetry; it does
not write another ACP checkpoint. Only lifecycle metadata, ACP session id, tool
name, status, and exception class are reported. Prompt text, thought text, tool
input, tool output, and ACP transcript/checkpoint contents are never placed in
the activity stream.

Raft's bridge drains activity on its wake-stream iteration. Consequently very
short phases can be delivered together, and the UI may not visibly dwell on
each intermediate state. This is a current External Agent bridge limitation,
not a missing ACP event: DeerFlow's structured thought, message, and tool-call
updates are all mapped by the adapter.

If Raft reports that a reply's delivery state is unknown after a server error,
the adapter records the inbox row as `delivery_unknown` and does not resend it.
This avoids duplicate replies when the server may already have committed the
original send. Reconcile these rows manually instead of automatically using
`--send-draft`.

For an intentional top-level DM reply, the adapter passes
`--target-confirmed`. If Raft explicitly returns `SEND_HELD_AS_DRAFT` with
`effect=draft_saved` (which guarantees no delivery occurred), the adapter
immediately confirms that unchanged body with `--send-draft`; ambiguous
delivery failures remain quarantined and are never auto-confirmed.

Top-level channel messages receive their answer in a new thread. Top-level DMs
are answered directly in the main DM, while messages already inside a thread
stay in that thread. Raft may expose one DM through both participants' target
aliases; the adapter normalizes and deduplicates those aliases before invoking
DeerFlow.

For a one-shot connectivity check without the long-lived Raft wake bridge:

```powershell
uv run raft-deerflow-adapter --config .\adapter.toml --once
```

## Current scope

- Text inbox messages and text/ResourceLink ACP output are supported.
- Raft attachment downloading is not implemented yet; the textual attachment
  marker still reaches DeerFlow.
- ACP permission requests are automatically approved.
- Messages acknowledged by the Raft CLI are first stored in SQLite; failed ACP
  turns remain pending and are retried on the next wake/poll.
- Generated ACP replies are persisted before Raft delivery. A known-failed send
  retries only the saved reply body and never reruns the ACP turn. Messages move
  to `failed` after `adapter.max_message_attempts` failures.
- One local workspace is shared by all Raft conversations, while ACP sessions
  remain separate.
