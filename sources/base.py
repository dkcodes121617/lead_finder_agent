"""The one interface every lead source implements.

A source that breaks is deleted from `SOURCES_ENABLED`; a new source is one
file. That is the whole design, and it is why every source returns the same
`SourceResult` regardless of whether it read a subreddit, a map, or a database
table.

## The hard rule

**This agent never writes to any platform.** No comment, no reply, no DM, no
vote, no submission — not immediately and not after a delay. That is not a
policy note here, it is a property of the code: sources expose read methods
only, and `tools/no_write_endpoints.py` fails the build if a write, vote or DM
endpoint path ever appears anywhere in this repo.

A rule a human has to remember will eventually be forgotten by a human. A
failing build will not.

## Failure is per-source and never fatal

One dead vendor must never take a run to zero. Every source returns
`ok=False` with a reason instead of raising, the runner records the failure
against that source's cursor row, and the other six carry on.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from wizcore.db.identity import try_entity_key

log = logging.getLogger("lead_finder.sources")


@dataclass
class Candidate:
    """One raw thing a source found, before classification.

    `source_uid` must be stable for the same item across runs — it is half of
    `UNIQUE (source, source_uid)`, which is what makes re-scanning a subreddit a
    no-op rather than a pile of duplicates.
    """

    source: str
    source_uid: str
    title: str = ""
    body: str = ""
    url: str = ""
    author: str = ""
    posted_at: datetime | None = None

    # ── identity hints, in the order entity_key prefers them ──
    business_name: str = ""
    domain: str = ""
    email: str = ""
    country: str = ""
    industry: str = ""
    # A URL that identifies the PROSPECT'S OWN BUSINESS — set by the discovery
    # sources (Places, OSM, RSS) and by nobody else.
    #
    # This is deliberately NOT `url`. On a social source `url` is wherever the
    # post points, which is usually not the poster: a Reddit thread linking to
    # techcrunch.com would key to domain:techcrunch.com, merging every redditor
    # who ever linked TechCrunch into one entity — and that entity's 90-day
    # cooldown would then suppress all of them after a single contact.
    #
    # A wrong merge silently removes real prospects; a missed merge only costs a
    # duplicate entity. So social sources fall back to source:author and the
    # identity URL stays empty.
    identity_url: str = ""

    # A source may pre-seed intent. Inbound does: someone who filled in a form on
    # your own site outranks the best thread scraping will ever find, and the
    # queue sorts by score, so that ordering falls out of the schema rather than
    # needing logic anywhere.
    seed_score: int | None = None
    # Set when a source already knows this is a lead and no LLM call is needed.
    presumed_lead: bool = False
    raw: dict = field(default_factory=dict)

    def entity_key(self) -> str | None:
        """None when nothing identifiable was found — caller skips the row."""
        return try_entity_key(
            domain=self.domain,
            url=self.identity_url,
            email=self.email,
            source=self.source,
            author=self.author,
        )

    def text_for_classifier(self, limit: int = 1200) -> str:
        parts = [self.title.strip(), self.body.strip()]
        return "\n".join(p for p in parts if p)[:limit]


@dataclass
class SourceResult:
    source: str
    candidates: list[Candidate] = field(default_factory=list)
    cursor: str = ""
    ok: bool = True
    error: str = ""
    calls_made: int = 0

    @classmethod
    def failed(cls, source: str, error: str) -> SourceResult:
        return cls(source=source, ok=False, error=error[:500])


class LeadSource(ABC):
    """Read-only. There is no write method on this class and there never will be."""

    name: str = "base"
    # Providers this source spends against, for the budget guard.
    provider: str = ""

    def __init__(self, config, budget=None):
        self.config = config
        self.budget = budget

    @abstractmethod
    def fetch(self, cursor: str = "") -> SourceResult:
        """Return whatever is new since `cursor`. Must not raise."""

    # ── shared helpers ──
    def _client(self, *, headers: dict | None = None, timeout: float | None = None, **kw):
        """An httpx client with this agent's UA, plus whatever the source adds.

        `headers` and `timeout` are explicit parameters rather than part of
        `**kw` on purpose. Passing them through **kw made every call that
        supplied its own — an Authorization header, or Overpass's longer
        timeout — raise `got multiple values for keyword argument`, which the
        source then reported as a failed fetch. Three of the seven sources were
        dead that way, and each one looked like a vendor problem.
        """
        merged = {"user-agent": "wizcodes-leadfinder/1.0 (+https://wizcodes.site)"}
        merged.update(headers or {})
        return httpx.Client(
            timeout=timeout or self.config.http_timeout,
            follow_redirects=True,
            headers=merged,
            **kw,
        )

    def _afford(self, units: int = 1, cost: float = 0.0) -> bool:
        """False when this source's daily budget is spent.

        Skipping is the right response, not aborting: one metered vendor running
        out must never cost the run its six free sources.
        """
        if not (self.budget and self.provider):
            return True
        return self.budget.afford(self.provider, units, cost)


def to_utc(value) -> datetime | None:
    """Parse the several shapes vendors call a timestamp. None if unparseable.

    Sources hand back unix seconds, unix milliseconds, and ISO strings with and
    without a 'Z'. Getting this wrong does not raise — it just silently makes
    `posted_at` wrong, which then makes the lookback window wrong.
    """
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        # Anything past ~2286 in seconds is really milliseconds.
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            # The last format carries no zone by design: some feeds publish a
            # bare local timestamp. Assuming UTC is the only defensible reading
            # and is what the explicit tzinfo below applies — a naive datetime
            # must never escape this function, because comparing one against an
            # aware datetime raises, and the lookback window does exactly that.
            parsed = datetime.strptime(text, fmt)  # noqa: DTZ007
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
