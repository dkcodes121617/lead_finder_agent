"""Writing to `core` — the only place in this agent that does.

## The single-writer rule

**The Lead Finder is the sole writer of `core.leads`.** The Outreach agent is a
pure consumer: it claims rows through `core.claim_leads()` and never inserts
one. That is what turns "no duplicates" from a promise into a property of a
UNIQUE constraint, and it is why business discovery lives in this agent rather
than in Outreach where an earlier plan put it.

## One connection, one transaction

Neon's free plan bills a 5-minute idle window per wake-up, so every source
opening its own connection risks paying that window seven times. Everything here
happens in one short connection at the end of the run.

The transaction boundary is also correctness, not just cost. Inbound rows are
marked `processed_at` **in the same transaction as the lead inserts**, so a
crash between the two is impossible. Marking them earlier would silently lose
the highest-intent leads in the system: the row would look handled and no lead
would exist.
"""
from __future__ import annotations

import json
import logging

from wizcore.db.conn import connect

log = logging.getLogger("lead_finder.persist")

# Countries the campaign actually sells into. Used only for `region_tier`, which
# is a sort hint, never a filter — a lead from anywhere else is still a lead.
_PRIORITY = {
    "united states", "usa", "us", "united kingdom", "uk", "gb", "ireland", "ie",
    "germany", "de", "france", "fr", "netherlands", "nl", "belgium", "be",
    "spain", "es", "italy", "it", "sweden", "se", "norway", "no", "denmark",
    "dk", "finland", "fi", "austria", "at", "switzerland", "ch", "poland", "pl",
    "portugal", "pt", "canada", "ca", "australia", "au",
}


def region_tier(country: str) -> str:
    return "us_eu_priority" if (country or "").strip().lower() in _PRIORITY else "other"


def persist(config, candidates: list, verdicts: list, angles: dict[int, str]) -> dict:
    """Write entities and leads, mark inbound rows, return counters.

    `angles` maps candidate index -> reply angle, for the few that earned one.
    """
    counters = {
        "entities_new": 0,
        "leads_inserted": 0,
        "leads_duplicate": 0,
        "inbound_marked": 0,
        "skipped_unclassified": 0,
    }
    if not candidates:
        return counters

    inserted_lead_rows: list[dict] = []
    inbound_ids: list[int] = []

    with connect(config.database_url) as conn, conn.cursor() as cur:
        for index, candidate in enumerate(candidates):
            entity = candidate.raw.get("entity_key") or candidate.entity_key()
            if not entity:
                continue

            if _upsert_entity(cur, candidate, entity):
                counters["entities_new"] += 1

            lead = _insert_lead(
                cur, candidate, verdicts[index], entity, angles.get(index) or ""
            )
            if lead is None:
                counters["leads_duplicate"] += 1
            else:
                counters["leads_inserted"] += 1
                inserted_lead_rows.append(lead)

            if _is_unclassified(candidate, verdicts[index]):
                counters["skipped_unclassified"] += 1
            if candidate.raw.get("inbound_event_id"):
                inbound_ids.append(int(candidate.raw["inbound_event_id"]))

        # ── inbound bookkeeping, same transaction as the inserts above ──
        # A crash between "lead inserted" and "event marked" is therefore
        # impossible. Marking earlier would leave a row that looks handled with
        # no lead behind it, which is the silent loss this whole pipe exists to
        # prevent.
        if inbound_ids:
            cur.execute(
                "UPDATE core.inbound_events SET processed_at = now() "
                "WHERE id = ANY(%s) AND processed_at IS NULL",
                (inbound_ids,),
            )
            counters["inbound_marked"] = cur.rowcount

    counters["_rows"] = inserted_lead_rows   # not a counter; consumed by notify
    return counters


def _is_unclassified(candidate, verdict) -> bool:
    return not candidate.presumed_lead and verdict.is_lead is None


def _upsert_entity(cur, candidate, entity: str) -> bool:
    """Insert or refresh the business. True when this entity is new.

    `last_seen` always advances; identity fields fill in only when we learn
    something new. The COALESCE order is what enforces that — an existing value
    is never overwritten with a blank from a thinner source, so a Places row
    with a website cannot be flattened by a later Reddit row without one.
    """
    cur.execute(
        """
        INSERT INTO core.entities
            (entity_key, business_name, domain, country, region_tier, industry)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (entity_key) DO UPDATE SET
            last_seen     = now(),
            business_name = COALESCE(core.entities.business_name, EXCLUDED.business_name),
            domain        = COALESCE(core.entities.domain,        EXCLUDED.domain),
            country       = COALESCE(core.entities.country,       EXCLUDED.country),
            industry      = COALESCE(core.entities.industry,      EXCLUDED.industry)
        RETURNING (xmax = 0) AS inserted
        """,
        (
            entity,
            candidate.business_name or None,
            _domain_of(candidate) or None,
            candidate.country or None,
            region_tier(candidate.country),
            candidate.industry or None,
        ),
    )
    row = cur.fetchone()
    return bool(row and row.get("inserted"))


def _insert_lead(cur, candidate, verdict, entity: str, angle: str) -> dict | None:
    """Insert the lead. None when `(source, source_uid)` already existed.

    That conflict is the across-run dedup layer doing its job, not an error: it
    is what makes re-scanning the same subreddit every 30 minutes a no-op.
    """
    # A seeded score wins over the classifier's. Inbound rows are scored from
    # what the person actually did on the site, which is better evidence than a
    # model reading a summary of it.
    intent = candidate.seed_score if candidate.seed_score is not None else verdict.intent_score
    is_lead = True if candidate.presumed_lead else verdict.is_lead

    cur.execute(
        """
        INSERT INTO core.leads
            (source, source_uid, entity_key, url, title, body_snippet, author,
             posted_at, is_lead, confidence, service_line, intent_score,
             reasoning, reply_angle, raw)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (source, source_uid) DO NOTHING
        RETURNING lead_id, intent_score, title, url, service_line, confidence
        """,
        (
            candidate.source,
            candidate.source_uid,
            entity,
            candidate.url or None,
            candidate.title[:500] or None,
            candidate.body[:1000] or None,
            candidate.author[:200] or None,
            candidate.posted_at,
            is_lead,
            verdict.confidence,
            verdict.service_line,
            intent,
            verdict.reasoning or None,
            angle or None,
            json.dumps(candidate.raw, default=str),
        ),
    )
    lead = cur.fetchone()
    if lead is None:
        return None

    lead["entity_key"] = entity
    lead["business_name"] = candidate.business_name
    lead["source"] = candidate.source
    lead["reply_angle"] = angle
    # Only count a lead against the entity when one was actually inserted;
    # incrementing on a conflict would inflate lead_count every single run.
    cur.execute(
        "UPDATE core.entities SET lead_count = lead_count + 1 WHERE entity_key = %s",
        (entity,),
    )
    return lead


def _domain_of(candidate) -> str:
    from wizcore.db.identity import registrable_domain

    return candidate.domain or registrable_domain(candidate.identity_url)


def record_cursors(config, results: list) -> None:
    """Per-source health, so a persistently broken source is visible and skippable.

    Failure bookkeeping is best-effort: if this write fails, the run's actual
    work is already committed and losing a cursor update is not worth raising over.
    """
    try:
        with connect(config.database_url, autocommit=True) as conn, conn.cursor() as cur:
            for result in results:
                cur.execute(
                    """
                    INSERT INTO leadfind.source_cursors
                        (source, last_cursor, last_run_at, last_ok, last_error, fail_streak)
                    VALUES (%s, %s, now(), %s, %s, %s)
                    ON CONFLICT (source) DO UPDATE SET
                        last_cursor = COALESCE(EXCLUDED.last_cursor, leadfind.source_cursors.last_cursor),
                        last_run_at = now(),
                        last_ok     = EXCLUDED.last_ok,
                        last_error  = EXCLUDED.last_error,
                        -- reset on success, climb on failure: that streak is what
                        -- lets the runner mute a source that is simply gone.
                        fail_streak = CASE WHEN EXCLUDED.last_ok
                                           THEN 0
                                           ELSE leadfind.source_cursors.fail_streak + 1 END
                    """,
                    (
                        result.source,
                        result.cursor or None,
                        result.ok,
                        (result.error or None),
                        0 if result.ok else 1,
                    ),
                )
    except Exception:
        log.warning("could not record source cursors", exc_info=True)


def muted_sources(config) -> set[str]:
    """Sources that have failed `FAIL_STREAK_SKIP` times running.

    Skipped and alerted once, rather than alerted every 30 minutes forever —
    which is how an alert channel becomes something nobody reads.
    """
    try:
        with connect(config.database_url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT source FROM leadfind.source_cursors WHERE fail_streak >= %s",
                (config.fail_streak_skip,),
            )
            return {r["source"] for r in cur.fetchall()}
    except Exception:
        return set()
