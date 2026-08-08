"""X / Twitter, read-only, via twitterapis.com.

Same class of vendor as redditapis and the same discipline applies: metered, so
it goes through the budget guard, and swappable, so nothing else in the system
knows where the tweets came from.

Endpoint verified live by `tools/preflight.py`:

    GET {base}/twitter/tweet/advanced_search?query=...

Read endpoints only. No posting, no replying, no liking, no DMs — enforced by
`tools/no_write_endpoints.py`, not by remembering.
"""
from __future__ import annotations

import logging

from sources.base import Candidate, LeadSource, SourceResult, to_utc

log = logging.getLogger("lead_finder.sources.twitter")

_COST_PER_READ = 0.0008
_PATH = "/twitter/tweet/advanced_search"


class TwitterSource(LeadSource):
    name = "twitter"
    provider = "twitterapis"

    def fetch(self, cursor: str = "") -> SourceResult:
        base = self.config.twitterapis_base_url.rstrip("/")
        queries = self.config.twitter_queries
        if not queries:
            return SourceResult.failed(self.name, "TWITTER_QUERIES is empty")

        candidates: list[Candidate] = []
        calls = 0
        errors: list[str] = []
        try:
            with self._client(
                headers={"Authorization": f"Bearer {self.config.twitterapis_key}"}
            ) as client:
                for query in queries:
                    if not self._afford(1, _COST_PER_READ):
                        errors.append("daily twitterapis budget reached")
                        break
                    try:
                        # Quoted phrase + lang:en. Without the quotes the vendor
                        # returns anything sharing a word with the query, which
                        # is a lot of reads for a lot of noise.
                        resp = client.get(
                            f"{base}{_PATH}",
                            params={"query": f'"{query}" lang:en -is:retweet'},
                        )
                        calls += 1
                        if resp.status_code != 200:
                            errors.append(f"{query!r}: HTTP {resp.status_code}")
                            continue
                        for tweet in _tweets(resp.json()):
                            built = _to_candidate(tweet, query)
                            if built:
                                candidates.append(built)
                    except Exception as e:
                        errors.append(f"{query!r}: {e}")
        except Exception as e:
            return SourceResult.failed(self.name, str(e))

        if errors and not candidates:
            return SourceResult.failed(self.name, "; ".join(errors[:3]))
        return SourceResult(
            source=self.name, candidates=candidates, calls_made=calls,
            error="; ".join(errors[:3]),
        )


def _tweets(payload) -> list[dict]:
    """Find the tweet list wherever this vendor put it.

    Third-party mirrors of the X API disagree on the envelope — `tweets`,
    `data`, `results`, or a bare list — and the shape has changed before without
    notice. Checking the known keys costs nothing and turns a silent zero-result
    run into one that keeps working.
    """
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("tweets", "data", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [t for t in value if isinstance(t, dict)]
        if isinstance(value, dict) and isinstance(value.get("tweets"), list):
            return [t for t in value["tweets"] if isinstance(t, dict)]
    return []


def _to_candidate(tweet: dict, query: str) -> Candidate | None:
    tweet_id = str(tweet.get("id") or tweet.get("id_str") or tweet.get("rest_id") or "").strip()
    text = str(tweet.get("text") or tweet.get("full_text") or "").strip()
    if not (tweet_id and text):
        return None
    author_obj = tweet.get("author") or tweet.get("user") or {}
    handle = str(
        (author_obj.get("userName") if isinstance(author_obj, dict) else "")
        or (author_obj.get("screen_name") if isinstance(author_obj, dict) else "")
        or tweet.get("username")
        or ""
    ).strip().lstrip("@")

    return Candidate(
        source="twitter",
        source_uid=f"tw:{tweet_id}",
        title=text[:120],
        body=text,
        url=str(tweet.get("url") or (f"https://x.com/{handle}/status/{tweet_id}" if handle else "")),
        author=handle,
        posted_at=to_utc(tweet.get("createdAt") or tweet.get("created_at")),
        # identity_url unset on purpose — a handle identifies the person; a link
        # in their tweet does not. See Candidate.identity_url.
        raw={
            "query": query,
            "likes": tweet.get("likeCount") or tweet.get("favorite_count"),
            "replies": tweet.get("replyCount"),
        },
    )
