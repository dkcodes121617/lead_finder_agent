"""Graph assembly.

Shape:

    preflight ─┬─► inbound ──────┐
               ├─► hackernews ───┤
               ├─► reddit ───────┼─► collect ─► classify ─► persist ─► notify ─► END
               ├─► places ───────┤
               └─► ... ──────────┘
                (concurrent, independently failing)

The fan-out is the point. Seven sources run at once, so one slow vendor costs
latency rather than the run, and a dead one is skipped and reported while the
other six carry on.

## Per-source retry, tuned to the dependency

A single global retry setting is wrong for every source at once: the metered
vendors need few attempts (each retry is money), the free ones can afford more,
and our own database should barely retry at all because if it is down, nothing
else will work either. So `_RETRY` below carries a policy per source.

**Those policies are applied inside the source nodes, not by a
`RetryPolicy`.** A LangGraph RetryPolicy re-raises when its attempts run out,
and a node that raises fails the whole graph — measured here: one Overpass
hiccup took a run to zero, discarding six sources that had already succeeded.
"One dead vendor must never take a run to zero" outranks it, so the source
nodes retry internally and turn an exhausted retry into `ok=False`. See
`make_source_node`.

The nodes that *should* fail the run when they cannot work — classify, persist —
keep real `RetryPolicy` objects, because there is no useful partial result to
preserve if they are broken.

## Durability

`"async"`, not `"sync"`. This agent takes no irreversible outward action — it
reads public sources and writes rows guarded by unique constraints, so a
checkpoint written slightly late cannot cause a duplicate post or a duplicate
email. The two agents that *do* publish and send use `"sync"`.
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from graph.nodes import (
    make_classify,
    make_collect,
    make_notify,
    make_persist,
    make_preflight,
    make_source_node,
)
from graph.state import LeadFinderState

log = logging.getLogger("lead_finder.graph.build")

# Attempts and backoff per source, chosen from what each dependency costs and
# how it fails. `initial_interval` is seconds.
_RETRY: dict[str, dict] = {
    # Our own Postgres. If it is unreachable, retrying inside one run rarely
    # helps and the next cron is 30 minutes away regardless.
    "inbound": {"max_attempts": 2, "initial_interval": 2.0, "backoff_factor": 2.0},
    # Free and generally reliable; Algolia occasionally 503s under load.
    "hackernews": {"max_attempts": 4, "initial_interval": 2.0, "backoff_factor": 2.0},
    # Metered: every retry is a real read charged to us.
    "reddit": {"max_attempts": 2, "initial_interval": 4.0, "backoff_factor": 2.0},
    "twitter": {"max_attempts": 2, "initial_interval": 4.0, "backoff_factor": 2.0},
    # Free within quota, and quota errors are permanent for the day anyway.
    "stackexchange": {"max_attempts": 3, "initial_interval": 3.0, "backoff_factor": 2.0},
    # Enterprise-billed per call. Retry once, reluctantly.
    "places": {"max_attempts": 2, "initial_interval": 5.0, "backoff_factor": 2.0},
    # A free shared community endpoint that is genuinely slow under load. Be
    # patient rather than aggressive - hammering it is how an IP gets blocked.
    "osm": {"max_attempts": 3, "initial_interval": 10.0, "backoff_factor": 2.0},
    "rss": {"max_attempts": 3, "initial_interval": 3.0, "backoff_factor": 2.0},
}

_DEFAULT_RETRY = {"max_attempts": 3, "initial_interval": 3.0, "backoff_factor": 2.0}


def retry_spec(name: str) -> dict:
    """This source's attempts and backoff, applied inside its node."""
    return _RETRY.get(name, _DEFAULT_RETRY)


def build_graph(config, budget, checkpointer=None):
    graph = StateGraph(LeadFinderState)

    graph.add_node("preflight", make_preflight(config))
    graph.add_edge(START, "preflight")

    active = config.active_sources()
    if "inbound" in active:
        active = ["inbound"] + [s for s in active if s != "inbound"]

    for name in active:
        node = f"source_{name}"
        # No retry_policy here on purpose — see the module docstring. These
        # nodes must never raise, so a policy would have nothing to catch and
        # would only reintroduce the "one source kills the run" failure.
        graph.add_node(node, make_source_node(config, name, budget, retry_spec(name)))
        graph.add_edge("preflight", node)
        graph.add_edge(node, "collect")

    graph.add_node("collect", make_collect(config))
    graph.add_node(
        "classify",
        make_classify(config, budget),
        # The classifier already falls back from Groq to the proxy internally, so
        # a retry here only covers the case where both were briefly unreachable.
        retry_policy=RetryPolicy(max_attempts=2, initial_interval=5.0),
    )
    graph.add_node(
        "persist",
        make_persist(config),
        # Worth retrying: everything upstream has already been paid for, and
        # every write is idempotent under a unique constraint.
        retry_policy=RetryPolicy(max_attempts=3, initial_interval=2.0, backoff_factor=2.0),
    )
    graph.add_node("notify", make_notify(config))

    graph.add_edge("collect", "classify")
    graph.add_edge("classify", "persist")
    graph.add_edge("persist", "notify")
    graph.add_edge("notify", END)

    return graph.compile(checkpointer=checkpointer)


def make_checkpointer(config):
    """A PostgresSaver scoped to this agent's own schema, or None.

    The schema is per agent (`lf_ckpt`) and that is load-bearing: PostgresSaver
    keys rows by `thread_id` alone, so two agents sharing one schema would
    resume each other's runs on any collision.

    Returns None rather than raising if setup fails. This agent has no
    `interrupt()`, so a checkpointer is an optimisation — it saves re-paying for
    metered reads after a crash — not a requirement. Losing the whole run
    because the checkpoint tables would not initialise is the worse trade.
    """
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg import Connection

        schema = config.checkpoint_schema
        conn = Connection.connect(
            config.database_url,
            autocommit=True,
            prepare_threshold=0,      # PostgresSaver's own requirement
            options=f"-c search_path={schema},public",
        )
        saver = PostgresSaver(conn)
        saver.setup()
        log.info("checkpointer ready in schema %s", schema)
        return saver
    except Exception:
        log.warning("checkpointer unavailable; running without one", exc_info=True)
        return None
