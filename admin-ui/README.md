# DeerFlow Admin UI

Standalone static admin interface for the DeerFlow API service.

## Run

After the API service starts, visit:

```text
http://localhost:8000/management/
```

The FastAPI route only serves this page when `api.auth_enabled: true` is configured.
Requests to `/admin` are redirected to `/management/` for compatibility.

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
- `PUT /api/admin/models`: write model configuration to `config.yaml`.
- `POST /api/admin/config/reload`: reload file-backed configuration.
- `GET /api/admin/skills/custom`: list custom skills.
- `GET /api/admin/skills/custom/{name}`: read a custom skill.
- `PUT /api/admin/skills/custom/{name}`: create or update a custom skill.
- `DELETE /api/admin/skills/custom/{name}`: delete a custom skill.
- `GET /api/admin/skills/custom/{name}/history`: read sanitized skill history.
- `PUT /api/admin/skills/custom/{name}/files/{path}`: write supporting files.
- `DELETE /api/admin/skills/custom/{name}/files/{path}`: remove supporting files.
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

- Fine-grained model delete/default actions.
- Full runtime management for sandbox, tracing, Feishu, and scheduler lifecycle.
- MCP tool discovery using a real MCP session with operator approval.
