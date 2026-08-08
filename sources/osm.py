"""OpenStreetMap via Overpass — free, unmetered, and the volume source.

The original plan had Places carrying discovery and OSM as a supplement. The
costing inverts that: Places bills every website-bearing lookup at the
Enterprise tier (~1,000/month free), while Overpass is free and its `website=*`
tag is exactly the seed the weak-website assessment needs. At the volumes the
Outreach ramp eventually wants, **OSM has to be primary and Places the top-up.**

## Turning "dentists in Austin TX" into Overpass QL

`DISCOVERY_QUERIES` is written for Places, which takes plain language. Overpass
does not, so this file translates: the leading noun maps to an OSM tag through
`_TAGS`, and the trailing place name becomes an administrative area lookup.

That translation is lossy and it is meant to be. A query it cannot map is
skipped and reported rather than guessed at, because a wrong tag returns
confidently wrong businesses — a list of pharmacies when you asked for
physiotherapists — and nothing downstream would notice.
"""
from __future__ import annotations

import logging
import re
import time

from sources.base import Candidate, LeadSource, SourceResult

log = logging.getLogger("lead_finder.sources.osm")

# Overpass is a free service run by volunteers. Being rude to it is how an IP
# ends up blocked, which would cost the volume source this pipeline depends on.
_QUERY_GAP_SECONDS = 4.0
# Overpass's own guidance is to back off for a few seconds and retry. On a
# 30-minute schedule, waiting is free; losing the volume source is not.
_RETRY_WAIT_SECONDS = 12.0

# Business words -> OSM tags. Deliberately explicit: guessing a tag from a noun
# is how you end up emailing the wrong industry at scale.
_TAGS: dict[str, tuple[str, str]] = {
    "dentist": ("amenity", "dentist"),
    "dentists": ("amenity", "dentist"),
    "dental": ("amenity", "dentist"),
    "doctor": ("amenity", "doctors"),
    "doctors": ("amenity", "doctors"),
    "physiotherapist": ("healthcare", "physiotherapist"),
    "physiotherapists": ("healthcare", "physiotherapist"),
    "veterinary": ("amenity", "veterinary"),
    "vet": ("amenity", "veterinary"),
    "vets": ("amenity", "veterinary"),
    "restaurant": ("amenity", "restaurant"),
    "restaurants": ("amenity", "restaurant"),
    "cafe": ("amenity", "cafe"),
    "cafes": ("amenity", "cafe"),
    "hotel": ("tourism", "hotel"),
    "hotels": ("tourism", "hotel"),
    "lawyer": ("office", "lawyer"),
    "lawyers": ("office", "lawyer"),
    "law": ("office", "lawyer"),
    "solicitor": ("office", "lawyer"),
    "solicitors": ("office", "lawyer"),
    "accountant": ("office", "accountant"),
    "accountants": ("office", "accountant"),
    "estate": ("office", "estate_agent"),
    "realtor": ("office", "estate_agent"),
    "realtors": ("office", "estate_agent"),
    "architect": ("office", "architect"),
    "architects": ("office", "architect"),
    "plumber": ("craft", "plumber"),
    "plumbers": ("craft", "plumber"),
    "electrician": ("craft", "electrician"),
    "electricians": ("craft", "electrician"),
    "builder": ("craft", "builder"),
    "builders": ("craft", "builder"),
    "salon": ("shop", "hairdresser"),
    "hairdresser": ("shop", "hairdresser"),
    "hairdressers": ("shop", "hairdresser"),
    "gym": ("leisure", "fitness_centre"),
    "gyms": ("leisure", "fitness_centre"),
    "pharmacy": ("amenity", "pharmacy"),
    "pharmacies": ("amenity", "pharmacy"),
    "optician": ("shop", "optician"),
    "opticians": ("shop", "optician"),
}


class OsmSource(LeadSource):
    name = "osm"
    provider = ""   # free and unmetered

    def fetch(self, cursor: str = "") -> SourceResult:
        queries = self.config.discovery_queries
        if not queries:
            return SourceResult.failed(self.name, "DISCOVERY_QUERIES is empty")

        candidates: list[Candidate] = []
        calls = 0
        errors: list[str] = []
        try:
            # Overpass is a shared free service that can be slow under load, and
            # being rude to it is how an IP gets blocked. One request per query,
            # a real timeout, and a capped result set.
            with self._client(timeout=90) as client:
                for position, query in enumerate(queries):
                    parsed = _parse_query(query)
                    if not parsed:
                        errors.append(f"{query!r}: no OSM tag mapping")
                        continue
                    (key, value), area = parsed
                    # Overpass rate-limits by IP and answers 429 when a client
                    # arrives too fast. Measured: firing five queries back to
                    # back got three of them refused. A couple of seconds of
                    # politeness costs nothing on a 30-minute schedule and is
                    # the difference between five results and two.
                    if position:
                        time.sleep(_QUERY_GAP_SECONDS)
                    try:
                        resp, used = _post_with_retry(
                            client, self.config.overpass_url, _ql(key, value, area)
                        )
                        calls += used
                        if resp is None or resp.status_code != 200:
                            code = resp.status_code if resp is not None else "no response"
                            errors.append(f"{query!r}: HTTP {code}")
                            continue
                        for element in resp.json().get("elements", []):
                            built = _to_candidate(element, query, area)
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


def _post_with_retry(client, url: str, body: str, attempts: int = 3):
    """POST one Overpass query, retrying its own rate limiting.

    Retried here rather than at the node, because the failure is **per query**:
    the node-level retry would re-run all five queries to rescue the two that
    were refused, which is both slower and ruder to a free service.

    504 is included alongside 429 deliberately — a loaded Overpass instance
    answers slow queries with a gateway timeout, and that is a "come back in a
    moment", not a broken request.
    """
    response = None
    for attempt in range(1, attempts + 1):
        response = client.post(url, data={"data": body})
        if response.status_code not in (429, 502, 503, 504):
            return response, attempt
        if attempt < attempts:
            wait = _RETRY_WAIT_SECONDS * attempt
            log.info(
                "overpass HTTP %s, waiting %.0fs (attempt %d/%d)",
                response.status_code, wait, attempt, attempts,
            )
            time.sleep(wait)
    return response, attempts


def _parse_query(query: str) -> tuple[tuple[str, str], str] | None:
    """'dentists in Austin TX' -> (('amenity','dentist'), 'Austin')."""
    match = re.match(r"\s*(.+?)\s+in\s+(.+?)\s*$", query, re.I)
    if not match:
        return None
    subject, place = match.group(1), match.group(2)
    tag = None
    for word in re.findall(r"[a-z]+", subject.lower()):
        if word in _TAGS:
            tag = _TAGS[word]
            break
    if not tag:
        return None
    # Drop a trailing state/country code: OSM's administrative area is named
    # "Austin", not "Austin TX".
    city = re.sub(r"\s+(?:[A-Z]{2}|UK|US|USA|IE|IN)\s*$", "", place.strip())
    return tag, city.strip()


def _ql(key: str, value: str, area: str) -> str:
    """Overpass QL. `website` is required — a business with no site cannot be assessed.

    `nwr` covers nodes, ways and relations: a big practice is often mapped as a
    building outline rather than a point, and querying only nodes silently drops
    exactly the larger businesses worth contacting.
    """
    safe_area = area.replace('"', "").replace("\\", "")
    return f"""
[out:json][timeout:60];
area["name"="{safe_area}"]["boundary"="administrative"]->.searchArea;
nwr["{key}"="{value}"]["website"](area.searchArea);
out tags center 80;
""".strip()


def _to_candidate(element: dict, query: str, area: str) -> Candidate | None:
    tags = element.get("tags") or {}
    name = str(tags.get("name") or "").strip()
    website = str(tags.get("website") or tags.get("contact:website") or "").strip()
    if not (name and website):
        return None
    osm_id = f"{element.get('type', 'node')}/{element.get('id')}"
    address = " ".join(
        x for x in [
            str(tags.get("addr:housenumber") or ""),
            str(tags.get("addr:street") or ""),
            str(tags.get("addr:city") or area),
        ] if x
    ).strip()
    category = str(
        tags.get("amenity") or tags.get("shop") or tags.get("office")
        or tags.get("craft") or tags.get("healthcare") or ""
    )

    return Candidate(
        source="osm",
        source_uid=f"osm:{osm_id}",
        title=f"{name} - {category or 'business'}",
        body=f"{name}. {category}. {address}. Website: {website}",
        url=website,
        author=name,
        business_name=name,
        identity_url=website,
        email=str(tags.get("email") or tags.get("contact:email") or "").strip(),
        industry=category,
        raw={
            "query": query,
            "osm_id": osm_id,
            "address": address,
            "phone": tags.get("phone") or tags.get("contact:phone"),
        },
    )
