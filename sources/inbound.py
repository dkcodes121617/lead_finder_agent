"""Tier 0 — the website's own leads. The highest-intent source in the system.

Someone who typed their business problem into Wico AI, or filled in the contact
form, is warmer than anything scraping will ever produce: they are already on
the site, they have self-identified, and Wico's rows arrive with a classified
industry, a stated problem and a computed score attached.

Until the `wizcodes-inbound` Worker existed these landed in an inbox and
disappeared. Now the two site Workers append to `core.inbound_events` and this
source drains it.

## Two consumers, one table, no race

`core.inbound_events` has a single `processed_at` column and two readers:

    kind='contact_form' | 'wico_lead'   -> the Lead Finder (this file)
    kind='unsubscribe'                  -> the Outreach agent

They are partitioned **by kind and never overlap**, which is what makes one
flag safe for two consumers. If this source ever widened its filter to include
'unsubscribe', it would mark those rows processed and the Outreach agent would
never drain them into `core.suppressions` — people who asked to be removed would
silently keep receiving email. The filter is the safety property; do not relax it.

## Marking is not this source's job

`fetch()` reads and returns; it does not set `processed_at`. The row ids ride
along in `raw` and the persist step marks them **in the same transaction that
inserts the leads**. Marking here would mean a crash between read and insert
loses the best leads in the system, silently.
"""
from __future__ import annotations

import logging

from wizcore.db.conn import connect, fetch_all

from sources.base import Candidate, LeadSource, SourceResult, to_utc

log = logging.getLogger("lead_finder.sources.inbound")

# Only these two. See the module docstring — this tuple is a safety property.
LEAD_KINDS = ("contact_form", "wico_lead")


class InboundSource(LeadSource):
    name = "inbound"
    provider = ""   # free: it is our own database

    def fetch(self, cursor: str = "") -> SourceResult:
        try:
            with connect(self.config.database_url, autocommit=True) as conn:
                rows = fetch_all(
                    conn,
                    "SELECT id, kind, event_uid, payload, received_at "
                    "FROM core.inbound_events "
                    "WHERE processed_at IS NULL AND kind = ANY(%s) "
                    "ORDER BY received_at LIMIT %s",
                    (list(LEAD_KINDS), self.config.max_candidates_per_run),
                )
        except Exception as e:
            return SourceResult.failed(self.name, f"inbound drain failed: {e}")

        candidates: list[Candidate] = []
        for row in rows:
            try:
                built = (
                    _from_contact_form(row) if row["kind"] == "contact_form"
                    else _from_wico(row)
                )
            except Exception:
                # A malformed payload is one bad row, not a bad run. Leave
                # processed_at NULL so it is visible rather than silently gone.
                log.warning("skipping malformed inbound row %s", row.get("id"), exc_info=True)
                continue
            if built:
                candidates.append(built)

        return SourceResult(source=self.name, candidates=candidates, calls_made=0)


def _from_contact_form(row: dict) -> Candidate | None:
    p = row["payload"] or {}
    email = str(p.get("email") or "").strip()
    name = str(p.get("name") or "").strip()
    message = str(p.get("message") or "").strip()
    if not (email or message):
        return None
    project_type = str(p.get("projectType") or "").strip()
    return Candidate(
        source="inbound",
        # Namespaced by kind so a contact_form and a wico_lead that somehow
        # shared an event_uid cannot collide on UNIQUE (source, source_uid).
        source_uid=f"contact:{row['event_uid']}",
        title=f"Contact form: {project_type or 'enquiry'} from {name or email}",
        body=message,
        url="https://wizcodes.site/contact",
        author=name or email,
        posted_at=to_utc(p.get("received_at")) or row["received_at"],
        business_name=name,
        email=email,
        country=str(p.get("country") or ""),
        industry=project_type,
        # Self-identified buyer on our own site. Nothing outranks this.
        seed_score=95,
        presumed_lead=True,
        raw={"inbound_event_id": row["id"], "kind": row["kind"], "payload": p},
    )


def _from_wico(row: dict) -> Candidate | None:
    p = row["payload"] or {}
    contact = p.get("contact") or {}
    email = str(contact.get("email") or "").strip()
    problem = str(p.get("problem") or "").strip()
    industry = str(p.get("industry") or "").strip()
    if not (email or problem):
        return None

    # Wico computes its own score from real conversation signals (confidence,
    # stated timeline, stated budget). Trust it as a floor rather than
    # recomputing from a summary that has already lost that detail.
    wico_score = int(p.get("score") or 0)
    seed = 90 if email else max(75, min(90, 60 + wico_score // 2))

    return Candidate(
        source="inbound",
        source_uid=f"wico:{row['event_uid']}",
        title=f"Wico conversation: {industry or 'visitor'}"
              + (f" (intent {p.get('intent')})" if p.get("intent") else ""),
        body="\n".join(
            x for x in [problem, str(p.get("blueprint_summary") or "").strip()] if x
        ),
        url="https://wizcodes.site",
        author=email or "wico visitor",
        posted_at=to_utc(p.get("received_at")) or row["received_at"],
        email=email,
        country=str(p.get("country") or ""),
        industry=industry,
        seed_score=seed,
        presumed_lead=True,
        raw={"inbound_event_id": row["id"], "kind": row["kind"], "payload": p},
    )
