"""Graph state.

`source_results` carries an `operator.add` reducer because the source nodes fan
out and run **concurrently**. Two nodes returning a plain list for the same key
is a lost update: LangGraph applies the last write and the other source's
candidates vanish with no error anywhere. That bug only appears once two sources
happen to finish together, which is to say under load, in production, on a
schedule nobody is watching.

Everything else is written by exactly one node, so it needs no reducer.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class LeadFinderState(TypedDict, total=False):
    run_id: str
    started_at: str

    # Fanned in from the concurrent source nodes — reducer required.
    source_results: Annotated[list[Any], operator.add]

    # Written by exactly one node each.
    candidates: list[Any]
    verdicts: list[Any]
    reply_angles: dict[int, str]
    facts_block: str
    muted_sources: list[str]
    counters: dict[str, Any]
    # Rows actually inserted, handed from persist to notify. Declared here
    # because LangGraph rejects an update naming a key the schema does not have
    # — a state key is part of the contract, not an ad-hoc dict entry.
    inserted_rows: list[Any]
    notified: int
    aborted: str
