"""Tiny .env reader (no dependency): KEY=VALUE lines, optional quotes, # comments."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = ".env", override: bool = False) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    out = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
        if override or k not in os.environ:
            os.environ[k] = v
    return out
