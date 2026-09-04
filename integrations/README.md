# External integrations

This directory contains independently runnable integrations between DeerFlow
and external platforms. Each integration owns its packaging, dependency lock,
configuration, tests, and runtime state; integrations are not imported into or
bundled with the main `deerflow-api` Python package.

- `raft/`: Raft External Agent sidecar over ACP v1.

