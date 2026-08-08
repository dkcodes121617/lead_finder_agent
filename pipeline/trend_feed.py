"""Write harvested candidates into the Content Poster's trend store.

This agent already fetches Hacker News, Reddit, X and Stack Exchange every 30
minutes and throws away everything that is not a lead. Those same payloads are
exactly what the trend layer wants, so writing them on the way past costs **one
INSERT and zero API calls** — versus re-fetching two metered vendors to obtain
bytes we already had.

## The single-writer rule, and why this does not break it

`core.leads` is still Lead Finder only. Nothing about that changes.

What crosses is that this agent now writes into another agent's schema:
`content.trend_items`. Acceptable because the properties that make the rule
worth having all still hold:

  - **one writer per table** — the Lead Finder is the sole writer of *both*
    `core.leads` and `content.trend_items`. No second writer appears anywhere.
  - **one direction** — this writes and never reads back, so there is no cycle
    between agents and no ordering requirement between their schedules.
  - **append-only** — `ON CONFLICT DO NOTHING`, so a re-run is a no-op and this
    can never corrupt what the Content Poster has already scored.

Failure is swallowed entirely. Lead finding is this agent's job; feeding trends
is a by-product, and a by-product must never cost the run its actual work.
"""
from __future__ import annotations

import json
import logging

from wizcore.db.conn import connect

log = logging.getLogger("lead_finder.trend_feed")

# Sources whose content is public discussion worth trend-scoring. `inbound` is
# excluded deliberately: those are our own visitors' private enquiries, and
# routing them into a content pipeline would be a privacy failure, not a
# feature. `places` and `osm` are business listings, not discussion.
TREND_SOURCES = frozenset({"hackernews", "reddit", "twitter", "stackexchange", "rss"})

# Below this, an item is one person talking to nobody. The trend layer scores
# for relevance later; this is only about not writing thousands of dead rows.
_MIN_SIGNAL = {
    "hackernews": 30,     # points
    "reddit": 25,         # upvotes
    "twitter": 20,        # likes
    "stackexchange": 3,   # score
}


def _signal(candidate) -> int:
    raw = candidate.raw or {}
    for key in ("points", "upvotes", "likes", "score"):
        value = raw.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def feed(config, results: list) -> dict:
    """Write trend-worthy candidates. Returns counters; never raises."""
    counters = {"trend_fed": 0, "trend_skipped": 0}
    rows: list[tuple] = []

    for result in results:
        if result.source not in TREND_SOURCES:
            continue
        threshold = _MIN_SIGNAL.get(result.source, 0)
        for candidate in result.candidates:
            if _signal(candidate) < threshold:
                counters["trend_skipped"] += 1
                continue
            rows.append(
                (
                    candidate.source,
                    candidate.source_uid[:200],
                    candidate.title[:500],
                    candidate.url or None,
                    (candidate.body or "")[:2000] or None,
                    (candidate.author or "")[:200] or None,
                    json.dumps(candidate.raw or {}, default=str),
                    candidate.posted_at,
                    _cluster_key(candidate.title),
                )
            )

    if not rows:
        return counters

    try:
        with connect(config.database_url) as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO content.trend_items
                    (source, external_id, title, url, summary, author,
                     signals, published_at, cluster_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                ON CONFLICT (source, external_id) DO NOTHING
                """,
                rows,
            )
            counters["trend_fed"] = len(rows)
    except Exception:
        # Swallowed on purpose. This agent's job is finding leads; if the
        # Content Poster's schema is missing or unreachable, that is the
        # Content Poster's problem and must not fail a lead run.
        log.warning("could not feed trend store", exc_info=True)
        counters["trend_fed"] = 0
    return counters


def _cluster_key(title: str) -> str | None:
    """Duplicated from the Content Poster's `trends.store.cluster_key`.

    Deliberately not shared through wizcore. It is ten lines, it is only
    meaningful to one consumer, and promoting it would put a Content Poster
    heuristic into the library both other agents load — failing the admission
    test in wizcore's README ("would two copies drifting be a *bug*?"). Two
    copies drifting here means slightly worse clustering, not a wrong answer.
    """
    import re

    stop = {
        "a", "an", "the", "and", "or", "but", "if", "then", "than", "that", "this",
        "is", "are", "was", "were", "be", "to", "of", "in", "on", "at", "by", "for",
        "with", "from", "as", "it", "its", "you", "your", "we", "our", "they",
        "their", "new", "now", "how", "why", "what", "when", "where", "who",
        "will", "would", "can", "could", "should", "just", "also", "more", "most",
        "very", "much", "some", "any", "no", "not", "says", "said", "all",
    }
    words = [
        w for w in re.findall(r"[a-z0-9][a-z0-9'+.-]*", (title or "").lower())
        if w not in stop and len(w) > 2
    ]
    if not words:
        return None
    return "-".join(sorted(sorted(words, key=len, reverse=True)[:5]))
