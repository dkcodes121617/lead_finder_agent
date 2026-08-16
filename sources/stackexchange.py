"""Stack Exchange — structured buyer-intent questions, free with a key.

The key does not authenticate anything; it raises the quota from 300/day to
10,000/day. Preflight reported `quota_remaining=9997`, so this source is
effectively unmetered for a run that reads a few pages.

What makes it worth having: a question tagged `shopify` asking how to do
something that is really a build request is a lead that states its own problem
in technical detail. That is a much better classifier input than a tweet.

Read-only: `/questions` and `/search/advanced`. Nothing here answers, comments
or votes.
"""
from __future__ import annotations

import html
import logging
import re
import time

from sources.base import Candidate, LeadSource, SourceResult, to_utc

log = logging.getLogger("lead_finder.sources.stackexchange")

_BASE = "https://api.stackexchange.com/2.3"


class StackExchangeSource(LeadSource):
    name = "stackexchange"
    provider = ""   # free within quota

    def fetch(self, cursor: str = "") -> SourceResult:
        sites = self.config.stackexchange_sites or ["stackoverflow"]
        tags = self.config.stackexchange_tags
        since = int(time.time()) - self.config.lookback_for("stackexchange") * 60

        candidates: list[Candidate] = []
        calls = 0
        errors: list[str] = []
        try:
            with self._client() as client:
                for site in sites:
                    params = {
                        "site": site,
                        "key": self.config.stackexchange_key,
                        "order": "desc",
                        "sort": "creation",
                        "fromdate": since,
                        "pagesize": 50,
                        # `withbody` is the difference between classifying a
                        # title and classifying the actual question.
                        "filter": "withbody",
                    }
                    if tags:
                        # Semicolons are OR here. AND would return almost nothing.
                        params["tagged"] = ";".join(tags)
                    try:
                        resp = client.get(f"{_BASE}/questions", params=params)
                        calls += 1
                        if resp.status_code != 200:
                            errors.append(f"{site}: HTTP {resp.status_code}")
                            continue
                        payload = resp.json()
                        if payload.get("quota_remaining") is not None:
                            log.info(
                                "stackexchange quota_remaining=%s", payload["quota_remaining"]
                            )
                        for item in payload.get("items", []):
                            built = _to_candidate(item, site)
                            if built:
                                candidates.append(built)
                    except Exception as e:
                        errors.append(f"{site}: {e}")
        except Exception as e:
            return SourceResult.failed(self.name, str(e))

        if errors and not candidates:
            return SourceResult.failed(self.name, "; ".join(errors[:3]))
        return SourceResult(
            source=self.name, candidates=candidates, calls_made=calls,
            error="; ".join(errors[:3]),
        )


def _to_candidate(item: dict, site: str) -> Candidate | None:
    qid = str(item.get("question_id") or "").strip()
    title = html.unescape(str(item.get("title") or "")).strip()
    if not (qid and title):
        return None
    owner = item.get("owner") or {}
    body = re.sub(r"<[^>]+>", " ", str(item.get("body") or ""))
    return Candidate(
        source="stackexchange",
        source_uid=f"se:{site}:{qid}",
        title=title,
        body=re.sub(r"\s+", " ", html.unescape(body)).strip()[:2000],
        url=str(item.get("link") or ""),
        author=str(owner.get("display_name") or "").strip(),
        posted_at=to_utc(item.get("creation_date")),
        raw={"site": site, "tags": item.get("tags"), "score": item.get("score")},
    )
