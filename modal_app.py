"""Modal deployment for the Lead Finder.

    modal deploy modal_app.py          (or: .\\deploy.ps1)
    modal run modal_app.py::manual     one run, now

## Why Modal rather than GitHub Actions

The two live agents run on Actions and that works — but Actions cron drifts
10-30 minutes under load, and "be first to reply" needs a punctual trigger.
Private-repo Actions minutes are also metered, while Modal's credit is not, and
this repo holds prospect PII so it cannot be public.

## Cadence: :00 and :30, not every 15 minutes

Neon's free plan bills a **5-minute idle window on every wake-up** that cannot
be shortened. At a 15-minute cadence this agent alone would burn roughly 66 of
the 100 free CU-hours per month — before the other two agents and their
checkpointers. At 30 minutes it is about 33, which leaves room for everything
else.

If 15 minutes is ever genuinely wanted, the answer is not a shorter cron: keep
the dedup cache in a `modal.Dict` and touch Postgres only when a run actually
produces a new lead. Most runs find nothing, and a run that finds nothing should
cost zero database.
"""
from __future__ import annotations

import modal

app = modal.App("wizcodes-leadfind")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements("requirements.txt")
    # wizcore is not on PyPI. During build-out it is the editable install from
    # ../wizcore; once it is tagged, this line goes away and the pinned git
    # dependency in requirements.txt takes over.
    .add_local_python_source("wizcore")
    .add_local_python_source("config", "main", "graph", "pipeline", "scoring", "sources")
)

# One secret per agent folder, regenerated from that folder's .env on every
# deploy. This container never receives META_PAGE_ACCESS_TOKEN or BREVO_API_KEY
# — it has no reason to hold a credential that can publish or send.
secret = modal.Secret.from_name("wizcodes-leadfind")


@app.function(
    image=image,
    secrets=[secret],
    # :00 and :30. Modal's scheduler is UTC; this cadence is timezone-agnostic.
    schedule=modal.Cron("0,30 * * * *"),
    # Generous relative to a normal run (~1-2 min) but bounded, so a hung vendor
    # cannot hold a container open indefinitely. The lease on any claimed row
    # expires independently.
    timeout=900,
    # Two overlapping runs are safe — UNIQUE (source, source_uid) makes the
    # writes idempotent — but there is no reason to pay for them.
    max_containers=1,
    retries=0,   # the graph does its own per-node retry, tuned per dependency
)
def scheduled() -> dict:
    from main import run_once

    return run_once()


@app.function(
    image=image,
    secrets=[secret],
    # 08:00 IST = 02:30 UTC. Modal's scheduler is UTC.
    schedule=modal.Cron("30 2 * * *"),
    timeout=300,
    retries=1,
)
def digest() -> dict:
    """One message a day covering all five agents.

    It lives here because this agent owns the `core` schema and runs most often,
    but it reports across the whole system. Everything it surfaces is a *silent*
    failure — a stopped agent, a stuck run, a blocked idempotency claim, a
    hand-over nobody posted — because those are the only things worth a daily
    interruption.
    """
    from config import CONFIG
    from pipeline.digest import send_digest

    return {"sent": send_digest(CONFIG)}


@app.function(image=image, secrets=[secret], timeout=900)
def manual(dry_run: bool = True, sources: str = "") -> dict:
    """Ad-hoc run: `modal run modal_app.py::manual --dry-run true`.

    Defaults to the safe path. A manual trigger is exactly when someone is
    experimenting, which is exactly when the default should not publish.
    """
    import dataclasses

    from config import CONFIG
    from main import run_once

    config = CONFIG
    overrides = {}
    if dry_run:
        overrides["dry_run"] = True
    if sources:
        overrides["sources_enabled"] = [s.strip() for s in sources.split(",") if s.strip()]
    if overrides:
        config = dataclasses.replace(config, **overrides)
    return run_once(config)


@app.local_entrypoint()
def cli(dry_run: bool = True, sources: str = ""):
    print(manual.remote(dry_run=dry_run, sources=sources))
