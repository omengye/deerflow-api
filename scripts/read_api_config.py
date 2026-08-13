"""Read a simple value from config.yaml's api section.

Usage:
    python scripts/read_api_config.py port 8000
    python scripts/read_api_config.py host 127.0.0.1
"""

from __future__ import annotations

import sys
from pathlib import Path


def _read_with_yaml(path: Path, key: str, fallback: str) -> str:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    value = data.get("api", {}).get(key, fallback)
    if value is None or value == "":
        return fallback
    return str(value)


def _read_manually(path: Path, key: str, fallback: str) -> str:
    in_api = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            in_api = line.strip() == "api:"
            continue
        if not in_api:
            continue
        name, sep, value = line.strip().partition(":")
        if sep and name == key:
            value = value.strip().strip("\"'")
            return value or fallback
    return fallback


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("Usage: read_api_config.py <key> <fallback>", file=sys.stderr)
        return 2

    key, fallback = argv[1], argv[2]
    path = Path("config.yaml")
    if not path.exists():
        print(fallback)
        return 0

    try:
        print(_read_with_yaml(path, key, fallback))
    except Exception:
        print(_read_manually(path, key, fallback))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
