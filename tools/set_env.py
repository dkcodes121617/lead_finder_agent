"""Set a key in THIS agent's .env, safely.

    python tools/set_env.py GROQ_API_KEY=gsk_...
    python tools/set_env.py DRY_RUN=0

Operates only on the .env beside this agent. Nothing outside this folder is
read or written — that is the point of the layout.

Why this exists rather than an editor or a shell one-liner:

PowerShell 5.1's `Get-Content` defaults to the system ANSI codepage. Reading a
UTF-8 .env with it and writing back with `Set-Content -Encoding utf8` silently
double-encodes every non-ASCII byte: the comment headers turn to mojibake and a
BOM appears. The values survive (they are ASCII) so nothing fails loudly — the
file just quietly rots. That happened once already.

This reads and writes UTF-8 explicitly, never adds a BOM, preserves comments and
ordering, updates every occurrence of the key (a duplicate later in the file
would otherwise win at load time), and appends if absent.
"""
from __future__ import annotations

import sys
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"


def main() -> int:
    if len(sys.argv) != 2 or "=" not in sys.argv[1]:
        sys.exit("usage: python tools/set_env.py KEY=VALUE")
    key, value = sys.argv[1].split("=", 1)
    key = key.strip()

    if not ENV.exists():
        sys.exit(f"no .env at {ENV}")

    # utf-8-sig on read strips a BOM if some earlier tool added one; write plain
    # utf-8 so we never put one back.
    lines = ENV.read_text(encoding="utf-8-sig").splitlines()
    hits = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        if s.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            hits += 1

    if not hits:
        lines.append(f"{key}={value}")

    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  {'appended' if not hits else f'set ({hits}x)'}  {ENV.parent.name}/{key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
