"""RSS / Google Alerts — the cheapest marginal source there is.

Tier 3: off by default (`SOURCES_ENABLED` does not list it), here because
switching it on is one env var and it costs nothing. Point `RSS_FEEDS` at Google
Alerts feeds for phrases like "looking for a web developer" and it behaves like
any other source.

Parsed with the standard library rather than `feedparser`. Two formats and about
forty lines of XML walking is not worth a dependency in three container images —
and `xml.etree` refuses external entities by default, which is the one security
property that actually matters when parsing a stranger's XML.
"""
from __future__ import annotations

import logging
import re
from xml.etree import ElementTree

from sources.base import Candidate, LeadSource, SourceResult, to_utc

log = logging.getLogger("lead_finder.sources.rss")

_ATOM = "{http://www.w3.org/2005/Atom}"


class RssSource(LeadSource):
    name = "rss"
    provider = ""

    def fetch(self, cursor: str = "") -> SourceResult:
        feeds = self.config.rss_feeds
        if not feeds:
            return SourceResult.failed(self.name, "RSS_FEEDS is empty")

        candidates: list[Candidate] = []
        calls = 0
        errors: list[str] = []
        try:
            with self._client() as client:
                for feed in feeds:
                    try:
                        resp = client.get(feed)
                        calls += 1
                        if resp.status_code != 200:
                            errors.append(f"{feed[:50]}: HTTP {resp.status_code}")
                            continue
                        candidates.extend(_parse(resp.text, feed))
                    except Exception as e:
                        errors.append(f"{feed[:50]}: {e}")
        except Exception as e:
            return SourceResult.failed(self.name, str(e))

        if errors and not candidates:
            return SourceResult.failed(self.name, "; ".join(errors[:3]))
        return SourceResult(
            source=self.name, candidates=candidates, calls_made=calls,
            error="; ".join(errors[:3]),
        )


def _parse(xml_text: str, feed: str) -> list[Candidate]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as e:
        log.warning("unparseable feed %s: %s", feed[:60], e)
        return []

    items = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
    out: list[Candidate] = []
    for item in items:
        title = _text(item, "title") or _text(item, f"{_ATOM}title")
        link = _text(item, "link") or _attr(item, f"{_ATOM}link", "href")
        summary = (
            _text(item, "description")
            or _text(item, f"{_ATOM}summary")
            or _text(item, f"{_ATOM}content")
        )
        uid = _text(item, "guid") or _text(item, f"{_ATOM}id") or link
        if not (title and uid):
            continue
        out.append(
            Candidate(
                source="rss",
                source_uid=f"rss:{uid[:180]}",
                title=_clean(title),
                body=_clean(summary),
                url=link,
                # A feed entry points AT the business it is about, so unlike the
                # social sources this URL really is an identity.
                identity_url=link,
                posted_at=to_utc(
                    _text(item, "pubDate") or _text(item, f"{_ATOM}updated")
                ),
                raw={"feed": feed},
            )
        )
    return out


def _text(node, tag: str) -> str:
    found = node.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def _attr(node, tag: str, attr: str) -> str:
    found = node.find(tag)
    return (found.get(attr) or "").strip() if found is not None else ""


def _clean(text: str) -> str:
    import html

    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()
