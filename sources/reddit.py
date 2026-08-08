"""Reddit, read-only, via redditapis.com.

Reddit declined commercial use of its official API, so `redditapis.com` is the
live path and `praw` is the standby — which is why praw sits in
`requirements.txt` for a source that does not currently use it. Swapping is then
a config change (`REDDIT_BACKEND`) rather than a build under pressure at the
moment the vendor disappears.

## The endpoint took real work to find

Every guessable path 404s. `/api/*` returned a JSON `{"error":"Not found"}`
while `/reddit/*` returned Express's default HTML — so the base URL and the auth
layer were live and correct, and only the path was wrong. The real one is:

    GET {base}/api/reddit/posts?subreddit=&sort=&limit=&after=

It lives in `REDDITAPIS_POSTS_PATH` rather than hardcoded here, precisely
because the obvious guesses are all wrong and the next reader should not have to
rediscover that.

## Read methods only

There is no post, comment, vote, reply or DM method on this class, and there
never will be — `tools/no_write_endpoints.py` fails the build if one appears.
The rule is structural, not remembered.
"""
from __future__ import annotations

import logging

from sources.base import Candidate, LeadSource, SourceResult, to_utc

log = logging.getLogger("lead_finder.sources.reddit")

# ~$0.002 per read. Metered, so it goes through the budget guard.
_COST_PER_READ = 0.002


class RedditSource(LeadSource):
    name = "reddit"
    provider = "redditapis"

    def fetch(self, cursor: str = "") -> SourceResult:
        if self.config.reddit_backend != "redditapis":
            return SourceResult.failed(
                self.name, f"backend {self.config.reddit_backend!r} is not implemented"
            )

        base = self.config.redditapis_base_url.rstrip("/")
        path = self.config.redditapis_posts_path
        subreddits = self.config.subreddits
        if not subreddits:
            return SourceResult.failed(self.name, "SUBREDDITS is empty")

        candidates: list[Candidate] = []
        calls = 0
        errors: list[str] = []
        try:
            with self._client(
                headers={"Authorization": f"Bearer {self.config.redditapis_key}"}
            ) as client:
                for subreddit in subreddits:
                    if not self._afford(1, _COST_PER_READ):
                        errors.append("daily redditapis budget reached")
                        break
                    try:
                        resp = client.get(
                            f"{base}{path}",
                            params={"subreddit": subreddit, "sort": "new", "limit": 40},
                        )
                        calls += 1
                        if resp.status_code != 200:
                            errors.append(f"r/{subreddit}: HTTP {resp.status_code}")
                            continue
                        for post in resp.json().get("posts", []):
                            built = _to_candidate(post, subreddit)
                            if built:
                                candidates.append(built)
                    except Exception as e:
                        errors.append(f"r/{subreddit}: {e}")
        except Exception as e:
            return SourceResult.failed(self.name, str(e))

        if errors and not candidates:
            return SourceResult.failed(self.name, "; ".join(errors[:3]))
        return SourceResult(
            source=self.name,
            candidates=candidates,
            calls_made=calls,
            error="; ".join(errors[:3]),
        )


def _to_candidate(post: dict, subreddit: str) -> Candidate | None:
    post_id = str(post.get("id") or post.get("name") or "").strip()
    if not post_id:
        return None
    title = str(post.get("title") or "").strip()
    body = str(post.get("text") or "").strip()
    if not title:
        return None
    permalink = str(post.get("permalink") or "")
    if permalink and not permalink.startswith("http"):
        permalink = f"https://reddit.com{permalink}"

    return Candidate(
        source="reddit",
        source_uid=f"reddit:{post_id}",
        title=title,
        body=body,
        # The discussion, never post['url'] — on a link post that is wherever the
        # poster linked to, which is not the poster.
        url=permalink or str(post.get("url") or ""),
        author=str(post.get("author") or "").strip(),
        posted_at=to_utc(post.get("created_utc") or post.get("created")),
        # identity_url intentionally unset: see Candidate.identity_url. A
        # redditor is identified by their handle, not by what they linked to.
        raw={
            "subreddit": post.get("subreddit") or subreddit,
            "upvotes": post.get("upvotes"),
            "comments": post.get("comments"),
            "link": post.get("url"),
        },
    )
