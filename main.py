"""Lead Finder entry point — local CLI and the body of the Modal function.

    python main.py                 one run, honouring DRY_RUN from .env
    python main.py --dry-run       force the safe path regardless of .env
    python main.py --sources hackernews,inbound
    python main.py --once --verbose

`modal_app.py` calls `run_once()` directly, so the scheduled path and the local
path are the same code. If they were not, testing locally would prove nothing
about production.
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import uuid

from wizcore.db.runs import run_scope
from wizcore.db.spend import BudgetGuard
from wizcore.obs.log import log_event, setup_logging

from config import AGENT_NAME, CONFIG

log = logging.getLogger("lead_finder")


def run_once(config=CONFIG, run_id: str | None = None) -> dict:
    """One complete pass. Returns the run's counters."""
    run_id = run_id or str(uuid.uuid4())
    setup_logging(AGENT_NAME, run_id, config.log_level)

    from graph.build import build_graph, make_checkpointer
    from graph.nodes import alert_failure

    log_event(
        log, "run.start",
        dry_run=config.dry_run,
        sources=",".join(config.active_sources()),
    )

    budget = BudgetGuard.load(AGENT_NAME, config.budget_caps, config.database_url)
    checkpointer = make_checkpointer(config)

    try:
        with run_scope(AGENT_NAME, run_id, config.database_url) as recorder:
            graph = build_graph(config, budget, checkpointer)
            final = graph.invoke(
                {"run_id": run_id, "source_results": []},
                config={"configurable": {"thread_id": run_id}},
                # "async" is correct here: nothing this agent does is
                # irreversible, so a checkpoint written slightly late cannot
                # cause a duplicate anything. The publishing agents use "sync".
                durability="async",
            )
            counters = final.get("counters") or {}
            recorder.set(**{k: v for k, v in counters.items() if isinstance(v, int)})
            if counters.get("sources_failed"):
                recorder.mark_partial(f"{counters['sources_failed']} source(s) failed")
            log_event(log, "run.done", **{k: v for k, v in counters.items() if isinstance(v, int)})
            return counters
    except Exception as exc:
        log.exception("run failed")
        alert_failure(config, exc, run_id)
        raise
    finally:
        # Always flush the ledger, including on the error path — a run that
        # spent money and then crashed still spent it, and a budget that only
        # counts successful runs is not a budget.
        budget.flush()
        if checkpointer is not None:
            # The container is exiting anyway; a connection that will not close
            # cleanly is not worth failing an otherwise successful run over.
            with contextlib.suppress(Exception):
                checkpointer.conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="WizCodes Lead Finder")
    parser.add_argument("--dry-run", action="store_true",
                        help="force DRY_RUN regardless of .env")
    parser.add_argument("--sources", default="",
                        help="comma-separated override of SOURCES_ENABLED")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--once", action="store_true", help="accepted for symmetry; always one run")
    parser.add_argument("--digest", action="store_true",
                        help="print the daily digest and send it, then exit")
    args = parser.parse_args()

    if args.digest:
        from pipeline.digest import build, send_digest

        print(build(CONFIG))
        send_digest(CONFIG)
        return 0

    import dataclasses

    config = CONFIG
    overrides = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.sources:
        overrides["sources_enabled"] = [s.strip() for s in args.sources.split(",") if s.strip()]
    if args.verbose:
        overrides["log_level"] = "DEBUG"
    if overrides:
        config = dataclasses.replace(config, **overrides)

    try:
        counters = run_once(config)
    except Exception as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1

    print("\n--- run summary ---")
    for key in sorted(counters):
        print(f"  {key:22} {counters[key]}")
    if config.dry_run:
        print("\n  DRY_RUN=1 - nothing was written to core.leads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
