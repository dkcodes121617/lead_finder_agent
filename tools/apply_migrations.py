"""Apply THIS agent's migrations/*.sql to Neon, once, in order.

    python tools/apply_migrations.py            # apply anything pending
    python tools/apply_migrations.py --status   # show what has run

Reads NEON_DATABASE_URL from this agent's own .env (or the environment, which
wins — that is how Modal and CI inject it).

Three things a raw `psql -f` would not give, and each has bitten a real system:

  1. A LEDGER. `public.schema_migrations` records which files have run, keyed
     `<agent>/<filename>` so two agents cannot collide on a shared name. Re-running
     is a no-op instead of a bet on every statement being idempotent.
  2. AN ADVISORY LOCK. Two agents booting the same minute must not both try to
     create the same object. pg_advisory_lock serialises them.
  3. A TRANSACTION PER FILE. A migration that fails half way leaves nothing
     behind, rather than a schema that is half of two versions.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

AGENT_ROOT = Path(__file__).resolve().parent.parent
AGENT = AGENT_ROOT.name
MIGRATIONS = AGENT_ROOT / "migrations"
# Arbitrary but FIXED: every process that migrates this database must use the
# same number or the lock protects nothing.
LOCK_ID = 0x7712C0DE


def database_url() -> str:
    url = os.getenv("NEON_DATABASE_URL", "").strip()
    if url:
        return url
    env = AGENT_ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith("NEON_DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    sys.exit("NEON_DATABASE_URL not set (checked environment and ./.env)")


def files() -> list[Path]:
    if not MIGRATIONS.exists():
        return []
    return sorted(MIGRATIONS.glob("*.sql"), key=lambda p: p.name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    all_files = files()
    if not all_files:
        print(f"migrations: {AGENT} owns no schema - nothing to do")
        return 0

    with psycopg.connect(database_url(), autocommit=False) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS public.schema_migrations (
                   filename   text PRIMARY KEY,
                   applied_at timestamptz NOT NULL DEFAULT now())"""
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM public.schema_migrations")
            done = {r[0] for r in cur.fetchall()}

        if args.status:
            for f in all_files:
                key = f"{AGENT}/{f.name}"
                print(f"  {'applied' if key in done else 'PENDING'}  {key}")
            return 0

        pending = [f for f in all_files if f"{AGENT}/{f.name}" not in done]
        if not pending:
            print("migrations: nothing pending")
            return 0

        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        conn.commit()
        try:
            for path in pending:
                key = f"{AGENT}/{path.name}"
                print(f"applying {key} ...")
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO public.schema_migrations (filename) VALUES (%s)", (key,)
                )
                conn.commit()
                print(f"  ok {key}")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
            conn.commit()

    print("migrations: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
