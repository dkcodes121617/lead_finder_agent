"""Google Places (New) — cold business discovery.

## The cost fact that shapes this whole file

The research doc budgeted this against "Essentials-tier fields stay inside the
free 10,000 calls/month". That is wrong in a way that matters 10x:
**`websiteUri` is a Place Details Enterprise field, and a request bills at the
highest tier any requested field belongs to.** Since the website URL *is the
entire point* of the weak-website pipeline, every discovery call here is an
Enterprise call, and the free ceiling is roughly **1,000/month, not 10,000**.

That does not break the plan — 1,000 businesses a month is far more than 25
emails/day can consume — but it does mean the cap has to be enforced in code
rather than assumed. Hence `PLACES_MAX_CALLS_PER_RUN` *and* the daily budget
guard, and hence OpenStreetMap carrying the volume with Places as the top-up.

Unlike every other source here, these people have not asked for anything. That
is what the assessment step and the two-touch sequence in the Outreach agent are
for; this source's only job is to find real businesses with real websites.
"""
from __future__ import annotations

import logging

from sources.base import Candidate, LeadSource, SourceResult

log = logging.getLogger("lead_finder.sources.places")

_URL = "https://places.googleapis.com/v1/places:searchText"

# Request exactly what is needed and nothing more. Every extra field is a chance
# to pull the request into a higher billing tier — and `websiteUri` has already
# pulled it into the highest one.
_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.websiteUri",
        "places.primaryTypeDisplayName",
        "places.nationalPhoneNumber",
    ]
)


class PlacesSource(LeadSource):
    name = "places"
    provider = "places"

    def fetch(self, cursor: str = "") -> SourceResult:
        queries = self.config.discovery_queries
        if not queries:
            return SourceResult.failed(self.name, "DISCOVERY_QUERIES is empty")

        budget_left = self.config.places_max_calls_per_run
        candidates: list[Candidate] = []
        calls = 0
        errors: list[str] = []
        try:
            with self._client(
                headers={
                    "X-Goog-Api-Key": self.config.google_places_api_key,
                    "X-Goog-FieldMask": _FIELD_MASK,
                    "content-type": "application/json",
                }
            ) as client:
                for query in queries:
                    if calls >= budget_left:
                        errors.append(f"PLACES_MAX_CALLS_PER_RUN ({budget_left}) reached")
                        break
                    if not self._afford(1):
                        errors.append("daily places budget reached")
                        break
                    try:
                        resp = client.post(
                            _URL,
                            json={"textQuery": query, "pageSize": 20},
                        )
                        calls += 1
                        if resp.status_code != 200:
                            errors.append(f"{query!r}: HTTP {resp.status_code} {resp.text[:120]}")
                            continue
                        for place in resp.json().get("places", []):
                            built = _to_candidate(place, query)
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


def _to_candidate(place: dict, query: str) -> Candidate | None:
    place_id = str(place.get("id") or "").strip()
    name = str((place.get("displayName") or {}).get("text") or "").strip()
    if not (place_id and name):
        return None
    website = str(place.get("websiteUri") or "").strip()
    address = str(place.get("formattedAddress") or "").strip()
    category = str((place.get("primaryTypeDisplayName") or {}).get("text") or "").strip()

    return Candidate(
        source="places",
        source_uid=f"places:{place_id}",
        title=f"{name} - {category or 'business'}",
        body=f"{name}. {category}. {address}."
             + (f" Website: {website}" if website else " No website listed."),
        url=website or f"https://www.google.com/maps/place/?q=place_id:{place_id}",
        author=name,
        business_name=name,
        # A discovery source is exactly where identity_url IS the business — the
        # whole row came from looking that business up.
        identity_url=website,
        industry=category,
        country=address.rsplit(",", 1)[-1].strip() if "," in address else "",
        # Cold. It has to earn its score from the assessment, not from being found.
        seed_score=None,
        raw={
            "query": query,
            "address": address,
            "phone": place.get("nationalPhoneNumber"),
            "has_website": bool(website),
        },
    )
