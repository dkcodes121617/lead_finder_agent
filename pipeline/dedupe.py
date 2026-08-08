"""In-run deduplication — the layer the database cannot provide.

Four layers stop the same business being worked twice. Two are in Postgres and
two are here:

  1. `UNIQUE (source, source_uid)`      database  - the same thread twice
  2. `entity_key`                       this file - the same business, two sources
  3. `core.suppressions`                Outreach  - unsubscribed, bounced, client
  4. 90-day cooldown in `claim_leads`   database  - resurfacing monthly

Layer 1 handles across-run duplicates for free. What it cannot handle is a
*single run* that finds the same dental practice on Reddit and in Google Places:
those are two different `(source, source_uid)` pairs, so both insert, and the
Outreach agent later claims both and pitches twice.

Collapsing them here is cheap. Doing it after the fact is not — by then two
emails have gone out.
"""
from __future__ import annotations

import logging

log = logging.getLogger("lead_finder.dedupe")


def dedupe_in_run(candidates: list) -> tuple[list, dict]:
    """Drop exact repeats and collapse one business appearing more than once.

    Returns `(kept, counters)`. Nothing is silently discarded — every drop is
    counted and the counters land in `core.agent_runs`, because a dedup rule
    that quietly eats real leads should be visible the moment it starts doing
    that.
    """
    seen_uid: set[tuple[str, str]] = set()
    best_by_entity: dict[str, object] = {}
    kept: list = []
    counters = {"dup_uid": 0, "dup_entity": 0, "no_identity": 0}

    for candidate in candidates:
        uid_key = (candidate.source, candidate.source_uid)
        if uid_key in seen_uid:
            counters["dup_uid"] += 1
            continue
        seen_uid.add(uid_key)

        entity = candidate.entity_key()
        if not entity:
            # Nothing identifiable at all. Skipping is correct: an unidentifiable
            # lead cannot be deduped, cannot be suppressed, and cannot be
            # contacted, so persisting it only adds noise to the queue.
            counters["no_identity"] += 1
            continue
        candidate.raw["entity_key"] = entity

        existing = best_by_entity.get(entity)
        if existing is None:
            best_by_entity[entity] = candidate
            kept.append(candidate)
            continue

        counters["dup_entity"] += 1
        # Same business, two sources in one run. Keep the richer row — the one
        # that already knows a website or an email — because that is what the
        # Outreach agent needs to assess and reach them.
        if _richness(candidate) > _richness(existing):
            kept[kept.index(existing)] = candidate
            best_by_entity[entity] = candidate

    if any(counters.values()):
        log.info("dedupe: kept %d of %d %s", len(kept), len(candidates), counters)
    return kept, counters


def _richness(candidate) -> int:
    """How useful this row is downstream. Higher wins a collision."""
    score = 0
    if candidate.email:
        score += 4
    if candidate.identity_url or candidate.domain:
        score += 3
    if candidate.business_name:
        score += 2
    if candidate.seed_score:
        # An inbound row beats a scraped one on any tie: it is the same business
        # having actually contacted us.
        score += 5
    if candidate.body:
        score += 1
    return score
