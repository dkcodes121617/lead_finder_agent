"""Hacker News via the Algolia API. Free, no key, no vendor risk.

Which is exactly why it is the first scraping source to switch on: it proves the
whole pipeline — fan-out, classification, dedup, notification — without a
credential, a bill, or a third party who can change their terms.

Audience skew is founders and technical people, so the leads that come out of it
are "someone building something needs help", not "local business has a bad
website". Both are real; they just score differently.
"""
from __future__ import annotations

import logging
import time

from sources.base import Candidate, LeadSource, SourceResult, to_utc

log = logging.getLogger("lead_finder.sources.hackernews")

# search_by_date, not search: relevance ranking would keep returning the same
# highly-upvoted threads every 30 minutes, and every one after the first is a
# dedup no-op that cost an API call.
_PATH = "/search_by_date"


class HackerNewsSource(LeadSource):
    name = "hackernews"
    provider = ""   # free and unmetered

    def fetch(self, cursor: str = "") -> SourceResult:
        base = self.config.hn_base_url.rstrip("/")
        queries = self.config.hn_queries or ["looking for developer"]
        # Overlap the window deliberately. A lookback exactly equal to the cron
        # interval loses anything posted during the seconds a run takes, and the
        # unique constraint makes the overlap free.
        since = int(time.time()) - self.config.lookback_for("hackernews") * 60

        candidates: list[Candidate] = []
        calls = 0
        errors: list[str] = []
        try:
            with self._client() as client:
                for query in queries:
                    try:
                        resp = client.get(
                            f"{base}{_PATH}",
                            params={
                                "query": query,
                                "tags": "(story,comment,ask_hn)",
                                "numericFilters": f"created_at_i>{since}",
                                "hitsPerPage": 40,
                            },
                        )
                        calls += 1
                        if resp.status_code != 200:
                            errors.append(f"{query!r}: HTTP {resp.status_code}")
                            continue
                        for hit in resp.json().get("hits", []):
                            built = _to_candidate(hit, query)
                            if built:
                                candidates.append(built)
                    except Exception as e:
                        # One bad query must not cost the other three.
                        errors.append(f"{query!r}: {e}")
        except Exception as e:
            return SourceResult.failed(self.name, str(e))

        if errors and not candidates:
            return SourceResult.failed(self.name, "; ".join(errors[:3]))
        return SourceResult(
            source=self.name,
            candidates=candidates,
            calls_made=calls,
            # Partial success is still success — say so, but do not fail the source.
            error="; ".join(errors[:3]),
        )


def _to_candidate(hit: dict, query: str) -> Candidate | None:
    object_id = str(hit.get("objectID") or "").strip()
    if not object_id:
        return None
    title = (hit.get("title") or hit.get("story_title") or "").strip()
    body = (hit.get("comment_text") or hit.get("story_text") or "").strip()
    if not (title or body):
        return None
    author = (hit.get("author") or "").strip()
    return Candidate(
        source="hackernews",
        source_uid=f"hn:{object_id}",
        title=title or f"HN comment by {author}",
        body=_strip_html(body),
        url=hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}",
        author=author,
        posted_at=to_utc(hit.get("created_at_i") or hit.get("created_at")),
        raw={"query": query, "points": hit.get("points"), "tags": hit.get("_tags")},
    )


def _strip_html(text: str) -> str:
    """HN comment bodies arrive as HTML fragments.

    A dependency-free unescape is enough here: the classifier reads this, not a
    browser, and the only thing that matters is that entities do not survive as
    '&#x27;' where a human wrote an apostrophe.
    """
    import html
    import re

    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()
