"""The source registry.

Adding a source is one file plus one line here. Removing a broken one is an edit
to `SOURCES_ENABLED` — no code change, no deploy, which is the point of the
interface being this narrow.
"""
from __future__ import annotations

from sources.base import Candidate, LeadSource, SourceResult
from sources.hackernews import HackerNewsSource
from sources.inbound import InboundSource
from sources.osm import OsmSource
from sources.places import PlacesSource
from sources.reddit import RedditSource
from sources.rss import RssSource
from sources.stackexchange import StackExchangeSource
from sources.twitter import TwitterSource

REGISTRY: dict[str, type[LeadSource]] = {
    "inbound": InboundSource,
    "hackernews": HackerNewsSource,
    "reddit": RedditSource,
    "twitter": TwitterSource,
    "stackexchange": StackExchangeSource,
    "places": PlacesSource,
    "osm": OsmSource,
    "rss": RssSource,
}


def build_sources(config, budget=None) -> list[LeadSource]:
    """Instantiate the enabled sources, in the order they are listed.

    Order matters for one reason: `inbound` should run first so the highest
    intent leads exist before anything else competes for the run's candidate
    budget.
    """
    ordered = config.active_sources()
    if "inbound" in ordered:
        ordered = ["inbound"] + [s for s in ordered if s != "inbound"]
    return [REGISTRY[name](config, budget) for name in ordered]


__all__ = ["REGISTRY", "Candidate", "LeadSource", "SourceResult", "build_sources"]
