# DeerFlow native ACP bridge

The native bridge supports three transport shapes while keeping the Python
`deerflow-acpd` process warm:

- default: ACP stdio to the local loopback daemon;
- `--gateway`: authenticated ACP HTTP + SSE to the local loopback daemon;
- `--remote URL`: ACP stdio to a remote ACP HTTP + SSE gateway.

## Start the local gateway

Set one stable, random token in the gateway process environment. The external
token is independent from the random token stored in the daemon's local
`endpoint.json` file.

```powershell
$env:DEER_FLOW_ACP_GATEWAY_TOKEN = '<at-least-32-random-characters>'
deerflow-acp --gateway `
  --config D:\Tools\deerflow-api\config.yaml `
  --workspace D:\ACP\workspace `
  --listen 127.0.0.1:8787
```

Every `POST`, SSE `GET`, and `DELETE` request to `/acp` must include:

```http
Authorization: Bearer <token>
```

The gateway replaces the remote `cwd` in ACP session requests with the fixed
local `--workspace` path. It does not synchronize either filesystem.

## Connect from a remote DeerFlow API

The remote mode is a stdio-to-HTTP transport adapter, so the existing Python
`spawn_agent_process` workflow can use it without implementing the ACP SSE
routing protocol itself.

```powershell
$env:DEER_FLOW_ACP_GATEWAY_TOKEN = '<the-same-gateway-token>'
deerflow-acp --remote http://gateway-host:8787/acp
```

For DeerFlow configuration, use the `remote_deerflow` example in
`config.example.yaml`. HTTP is sufficient for local development and transport
testing. Production transport hardening is intentionally outside the first two
implementation phases.

## RustFS artifacts

When `local_acp.artifacts.enabled` is true, only files explicitly presented by
the local agent are uploaded to the configured S3-compatible RustFS bucket.
The ACP client validates and downloads the resulting resource link into the
invoking thread's `/mnt/acp-workspace/<invocation-id>/` directory. HTTPS is
required by default. A trusted private-network HTTP endpoint can be enabled for
one agent with an exact `artifact_allowed_hosts` entry and
`artifact_allow_insecure_http: true`; redirects remain disabled and the size
and SHA-256 checks still apply.

Install the optional publisher dependency in the local ACP environment:

```powershell
uv sync --extra rustfs
```
