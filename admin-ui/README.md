# DeerFlow Admin UI

Standalone static admin interface for the DeerFlow API service.

## Run

After the API service starts, visit:

```text
http://localhost:8000/management/
```

The FastAPI route only serves this page when `api.auth_enabled: true` is configured.

You can also open `index.html` directly in a browser during local development.

The login form only asks for the Bearer token. When opened from the filesystem,
the API base URL defaults to:

```text
http://localhost:8000
```

When served by the API process, the API base URL is inferred from the current origin.

## Current API Coverage

- `GET /api/admin/me`: validate admin capability metadata.
- `GET /api/admin/config`: read redacted runtime/configuration state.
- `GET /api/admin/config/health`: validate configuration compatibility and report safe diagnostics.
- `PUT /api/admin/models`: compatibility endpoint for replacing model configuration.
- `POST /api/admin/models`: create one model.
- `PATCH /api/admin/models/{name}`: safely patch one model; omitted secrets are retained.
- `DELETE /api/admin/models/{name}`: delete one model and repair the default selection.
- `GET|PUT /api/admin/title`: read or update automatic title generation.
- `GET|PUT /api/admin/subagents`: read or update built-in/custom subagent configuration.
- `GET|PUT /api/admin/memory`: read or update global memory configuration.
- `GET|PUT /api/admin/summarization`: read or update conversation summarization configuration.
- `GET /api/admin/scheduled-tasks`: list persisted scheduled tasks without internal metadata.
- `DELETE /api/admin/scheduled-tasks/{task_id}`: delete a scheduled task and its stored execution records.
- `POST /api/admin/config/reload`: reload file-backed configuration.
- `GET /api/admin/skills/custom`: list custom skills.
- `GET /api/admin/skills/custom/{name}`: read a custom skill.
- `PUT /api/admin/skills/custom/{name}`: create or update a custom skill.
- `DELETE /api/admin/skills/custom/{name}`: delete a custom skill.
- `GET /api/admin/skills/custom/{name}/history`: read sanitized skill history.
- `PUT /api/admin/skills/custom/{name}/files/{path}`: write supporting files.
- `DELETE /api/admin/skills/custom/{name}/files/{path}`: remove supporting files.
- `GET /api/admin/evolution/signals`: list sanitized automatic-discovery Signal summaries.
- `GET /api/admin/evolution/signals/{signal_id}`: inspect one Signal and its bounded, sanitized tool errors.
- `DELETE /api/admin/evolution/signals/{signal_id}`: cancel a queued Signal when necessary and delete its record.
- `POST /api/admin/evolution/observability/cleanup`: delete cancellable Signals and completed probation records while preserving active monitoring and linked history.
- `POST /api/admin/evolution/proposals/archive-batch`: archive an explicit set of terminal Proposals while preserving linked history.
- `PATCH /api/admin/runtime`: write allowlisted runtime fields.
- `POST /api/admin/mcp/{name}/enable`: enable an MCP server.
- `POST /api/admin/mcp/{name}/disable`: disable an MCP server.
- `POST /api/admin/mcp/{name}/test`: validate/probe an MCP server.
- `GET /api/models`: list configured models.
- `GET /api/skills`: list skills.
- `POST /api/skills/{name}/enable`: enable a skill.
- `POST /api/skills/{name}/disable`: disable a skill.
- `GET /api/mcp/config`: read MCP configuration.
- `PUT /api/mcp/config`: write MCP configuration.
- `GET /health/ready`: readiness details.

The login form validates the entered token by calling `GET /api/models` with:

```http
Authorization: Bearer <token>
```

## Future Admin Work

Broader admin operations can be added later:

- Structured ACP-agent management and permission approval controls.
- Full runtime management for sandbox and scheduler lifecycle.
- Tracing and token-usage observability management.
- MCP tool discovery using a real MCP session with operator approval.
