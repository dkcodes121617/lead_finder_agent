"""The graph's nodes.

## How retry actually works here

Sources never raise. Graceful degradation beats a stack trace, and one dead
vendor must never take a run to zero — a rule that had to be enforced in code
rather than assumed, because the first version violated it: a LangGraph
`RetryPolicy` re-raises when its attempts run out, and a single Overpass hiccup
duly failed the whole graph, discarding six sources that had already succeeded.

So `make_source_node` retries internally using that source's own tuned attempts
and backoff, and converts an exhausted retry into `ok=False` **data**. The
distinction that makes the retry worth having is still enforced: only transient
errors are retried (timeouts, 5xx, 429, connection resets), never definitive
ones (a missing key, a 401, an empty query list). Retrying a 401 is just a
slower way to fail.

The downstream nodes — classify, persist — keep real `RetryPolicy` objects,
because if they cannot work there is no partial result worth preserving.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime

from wizcore.db.spend import BudgetGuard
from wizcore.facts.site import SiteReader
from wizcore.facts.snapshot import build_snapshot
from wizcore.obs.log import log_event
from wizcore.telegram.send import esc, send

from pipeline.dedupe import dedupe_in_run, drop_already_known
from pipeline.notify import notify_leads, notify_run_summary
from pipeline.persist import muted_sources, persist, record_cursors
from scoring.classify import Classifier, verdicts_summary
from sources import REGISTRY
from sources.base import SourceResult

log = logging.getLogger("lead_finder.graph")


# Substrings that mean "the vendor wobbled", as opposed to "the credential is
# wrong". Deliberately narrow: misclassifying a permanent failure as transient
# burns the retry budget on something that will never succeed.
_TRANSIENT = re.compile(
    r"HTTP 5\d\d|HTTP 429|timeout|timed out|connection|reset by peer|"
    r"temporarily unavailable|ReadError|ConnectError|PoolTimeout",
    re.I,
)


def is_transient(error: str) -> bool:
    return bool(error) and bool(_TRANSIENT.search(error))


def make_preflight(config):
    """Ping what the run depends on before it spends anything.

    The blog agent already proves the pattern: on a dead dependency, exit
    cheaply and leave the slot due. Getting to node nine before discovering the
    database is unreachable wastes eight nodes of budget.
    """

    def preflight(state):
        config.validate()

        muted = muted_sources(config)
        if muted:
            log.warning("muted sources (failing repeatedly): %s", ", ".join(sorted(muted)))

        # Facts are fetched once per run and cached in the reader. Only the
        # reply-angle node uses them, but fetching here means a broken
        # SITE_READ_TOKEN fails immediately and obviously rather than at the end.
        facts_block = ""
        try:
            reader = SiteReader(
                repo=config.site_repo,
                token=config.site_read_token,
                ref=config.site_branch,
                local_dir=config.site_local_dir or None,
            )
            snapshot = build_snapshot(reader)
            facts_block = snapshot.to_prompt_block(
                max_playbook_chars=2500, include_posts=False, include_playbook=False
            )
            log_event(
                log, "facts.loaded",
                projects=len(snapshot.projects),
                services=len(snapshot.services),
            )
        except Exception as e:
            # Degraded, not fatal: without facts the reply angle is skipped, but
            # classification and the queue still work. Losing a nice-to-have
            # must not cost the run its actual job.
            log.warning("site facts unavailable, reply angles disabled: %s", e)

        return {
            "started_at": datetime.now(UTC).isoformat(),
            "facts_block": facts_block,
            "muted_sources": sorted(muted),
            "source_results": [],
        }

    return preflight


def make_source_node(config, name: str, budget: BudgetGuard, retry: dict):
    """One node per enabled source. They run concurrently and fail independently.

    ## Why the retry loop lives here and not in a LangGraph RetryPolicy

    A `RetryPolicy` re-raises once its attempts are exhausted, and a node that
    raises fails the whole graph. That was measured, not theorised: a single
    Overpass hiccup took an entire run to zero, discarding the six sources that
    had already succeeded and every candidate they had found.

    That is exactly the outcome this design forbids — *one dead vendor must
    never take a run to zero*. Where graceful degradation and per-node retry
    conflict, degradation wins. So retries happen here, where an exhausted
    retry becomes **data** (`ok=False`) instead of an exception.

    The per-source tuning survives intact: `retry` carries this source's own
    attempts and backoff, which is what the tuning was for. Only the escape
    hatch moved.
    """
    attempts = max(1, int(retry.get("max_attempts", 3)))
    interval = float(retry.get("initial_interval", 3.0))
    factor = float(retry.get("backoff_factor", 2.0))

    def source_node(state):
        started = time.monotonic()

        def out_of_time() -> bool:
            return time.monotonic() - started > config.source_deadline_seconds

        if name in (state.get("muted_sources") or []):
            return {
                "source_results": [
                    SourceResult(source=name, ok=True, error="skipped: muted after repeated failures")
                ]
            }

        source = REGISTRY[name](config, budget)
        result = SourceResult.failed(name, "never attempted")

        for attempt in range(1, attempts + 1):
            try:
                result = source.fetch()
            except Exception as e:
                # A source raising at all is a bug in that source — its
                # interface says it must not — so record it rather than let it
                # escape and take the run with it.
                result = SourceResult.failed(name, f"raised {type(e).__name__}: {e}")
                log.warning("source %s raised on attempt %d", name, attempt, exc_info=True)

            if result.ok or attempt == attempts or not is_transient(result.error):
                break

            # The budget, not just the attempt count.
            #
            # "One dead vendor must never take a run to zero" held for a source
            # that FAILS. It did not hold for one that merely hangs: Overpass
            # went unreachable and each of its three queries sat on a 90s HTTP
            # timeout, three attempts deep. The node returned ok=False exactly
            # as designed — 15 minutes later, by which point Modal had already
            # cancelled the container at its 900s limit and the run was lost.
            # Every other source had finished in 16 seconds.
            #
            # So a source now gets a wall-clock budget as well as a retry count,
            # and giving up inside it is what keeps the other sources' work.
            if out_of_time():
                result = SourceResult.failed(
                    name,
                    f"gave up after {config.source_deadline_seconds}s "
                    f"(attempt {attempt}/{attempts}): {result.error[:120]}",
                )
                log.warning("source %s exceeded its time budget", name)
                break

            wait = interval * (factor ** (attempt - 1))
            log.info(
                "source %s transient failure (attempt %d/%d), retrying in %.1fs: %s",
                name, attempt, attempts, wait, result.error[:160],
            )
            time.sleep(wait)

        log_event(
            log, "source.done", source=name, ok=result.ok,
            found=len(result.candidates), calls=result.calls_made,
            # Without this, a failed source logged a clean-looking line and the
            # reason lived only in a Telegram message.
            error=result.error[:200] or None,
        )
        return {"source_results": [result]}

    return source_node


def _fair_share(candidates: list, cap: int) -> list:
    """Cap the run by taking a turn from each source, not by taking the newest.

    ## The bug this replaces

    The previous version sorted every candidate newest-first and sliced to the
    cap. Two things made that quietly fatal:

      * **Reddit is a firehose.** It returns hundreds of posts a run, all
        minutes old, so it occupied the whole of a 300-row cap on its own.
      * **Businesses have no timestamp.** A Google Places or OpenStreetMap
        result is a shop, not a post — `posted_at` is None, which sorted to the
        very bottom. They were cut before the classifier ever saw one.

    Measured against the live database after a week of running: reddit 378
    leads, hackernews 15, everything else **zero**. Not because those sources
    were broken — they fetched fine and reported candidates every run — but
    because the cap ate them. A dead source and a starved source look identical
    from the outside, which is why this ran for a week unnoticed.

    ## What it does instead

    Round-robin: one candidate from each source in turn until the cap is full.
    Within a source, newest first, so a source that is genuinely a feed still
    gives up its stalest rows first. A source with three candidates contributes
    all three; a source with four hundred waits its turn.

    The result is that the cap now limits *volume* without deciding *variety*,
    which was never its job.
    """
    if len(candidates) <= cap:
        return candidates

    from collections import defaultdict

    by_source: dict[str, list] = defaultdict(list)
    for candidate in candidates:
        by_source[candidate.source].append(candidate)
    for rows in by_source.values():
        rows.sort(key=lambda c: c.posted_at or datetime.min.replace(tzinfo=UTC), reverse=True)

    out: list = []
    queues = list(by_source.values())
    while len(out) < cap and any(queues):
        for rows in queues:
            if not rows:
                continue
            out.append(rows.pop(0))
            if len(out) >= cap:
                break
    return out


def make_collect(config):
    """Merge the fan-in, dedupe, and cap the run."""

    def collect(state):
        results = state.get("source_results") or []
        candidates = [c for r in results for c in r.candidates]

        # Feed the Content Poster's trend store on the way past. These payloads
        # were already fetched and are about to be discarded; writing them costs
        # one INSERT and no API calls. Never allowed to affect this run.
        from pipeline.trend_feed import feed

        trend_counters = feed(config, results)

        kept, dedupe_counters = dedupe_in_run(candidates)

        # Before the cap, not after: a duplicate that survives to the cap has
        # taken a slot a new candidate needed. This is also what keeps the
        # classifier off work the database has already answered.
        kept, known_counters = drop_already_known(config, kept)

        capped = _fair_share(kept, config.max_candidates_per_run)

        counters = {
            "candidates_raw": len(candidates),
            "candidates": len(capped),
            "dropped_by_cap": len(kept) - len(capped),
            **dedupe_counters,
            **known_counters,
            "sources_ok": sum(1 for r in results if r.ok),
            "sources_failed": sum(1 for r in results if not r.ok),
            **trend_counters,
        }
        log_event(log, "collect.done", **counters)
        return {"candidates": capped, "counters": counters}

    return collect


def make_classify(config, budget: BudgetGuard):
    def classify(state):
        candidates = state.get("candidates") or []
        if not candidates:
            return {"verdicts": [], "reply_angles": {}}

        classifier = Classifier(config, budget)

        # Inbound rows already know they are leads and carry a score derived
        # from what the person actually did on the site. Paying a model to
        # re-judge that is spending budget to get a worse answer.
        to_classify = [c for c in candidates if not c.presumed_lead]
        verdicts_for = dict(
            zip(
                (i for i, c in enumerate(candidates) if not c.presumed_lead),
                classifier.classify(to_classify),
                strict=False,
            )
        )

        from scoring.classify import Verdict

        verdicts = []
        for index, candidate in enumerate(candidates):
            if candidate.presumed_lead:
                verdicts.append(
                    Verdict(
                        is_lead=True,
                        confidence="high",
                        service_line=_guess_service_line(candidate),
                        intent_score=candidate.seed_score or 90,
                        reasoning="inbound: self-identified on wizcodes.site",
                        provider="seeded",
                    )
                )
            else:
                verdicts.append(verdicts_for[index])

        facts_block = state.get("facts_block") or ""
        angles: dict[int, str] = {}
        if facts_block:
            ranked = sorted(
                (i for i, v in enumerate(verdicts) if v.is_lead),
                key=lambda i: verdicts[i].intent_score or 0,
                reverse=True,
            )
            # Only the best few, and only above the threshold. This is the one
            # expensive model call in the agent.
            for index in ranked[: config.notify_max_per_run]:
                angle = classifier.reply_angle(candidates[index], verdicts[index], facts_block)
                if angle:
                    angles[index] = angle

        counters = dict(state.get("counters") or {})
        counters.update(verdicts_summary(verdicts))
        counters["reply_angles"] = len(angles)
        log_event(log, "classify.done", **{k: v for k, v in counters.items() if k != "by_provider"})
        return {"verdicts": verdicts, "reply_angles": angles, "counters": counters}

    return classify


def _guess_service_line(candidate) -> str:
    """Map an inbound enquiry onto a canonical service line.

    Canonical names come from the site, and they are also `core.leads`'
    CHECK constraint — so anything unrecognised has to be 'none' rather than a
    plausible-looking guess that fails the INSERT.
    """
    text = f"{candidate.industry} {candidate.title} {candidate.body}".lower()
    if any(w in text for w in ("mobile", "ios", "android", "app store", "flutter", "react native")):
        return "Mobile Apps"
    if any(w in text for w in ("ai", "automat", "chatbot", "agent", "llm", "machine learning")):
        return "AI Automation"
    if any(w in text for w in ("web", "site", "saas", "dashboard", "shop", "ecommerce", "platform")):
        return "Web Development"
    return "none"


def make_persist(config):
    def persist_node(state):
        candidates = state.get("candidates") or []
        verdicts = state.get("verdicts") or []
        if not candidates:
            return {"counters": state.get("counters") or {}}

        if config.dry_run:
            # The dry path stops exactly here and nowhere else: every node above
            # ran for real against real sources, so what a live run would write
            # is fully determined by what this one computed.
            leads = sum(1 for v in verdicts if v.is_lead)
            counters = dict(state.get("counters") or {})
            counters.update({"leads_inserted": 0, "would_insert": leads, "dry_run": 1})
            log_event(log, "persist.skipped", reason="DRY_RUN", would_insert=leads)
            return {"counters": counters}

        written = persist(config, candidates, verdicts, state.get("reply_angles") or {})
        rows = written.pop("_rows", [])
        counters = dict(state.get("counters") or {})
        counters.update(written)
        log_event(log, "persist.done", **written)
        return {"counters": counters, "inserted_rows": rows}

    return persist_node


def make_notify(config):
    def notify(state):
        counters = dict(state.get("counters") or {})
        results = state.get("source_results") or []

        rows = state.get("inserted_rows") or []
        if config.dry_run:
            # Show what WOULD have been alerted, built from the same data the
            # live path uses. A dry run whose notifications are silent proves
            # nothing about the notifications.
            rows = [
                {
                    "lead_id": None,
                    "intent_score": (c.seed_score if c.seed_score is not None else v.intent_score),
                    "title": c.title,
                    "url": c.url,
                    "service_line": v.service_line,
                    "confidence": v.confidence,
                    "source": c.source,
                    "reply_angle": (state.get("reply_angles") or {}).get(i, ""),
                }
                for i, (c, v) in enumerate(
                    zip(state.get("candidates") or [], state.get("verdicts") or [], strict=False)
                )
                if v.is_lead or c.presumed_lead
            ]

        notified = notify_leads(config, rows)
        notify_run_summary(config, counters, results, set(state.get("muted_sources") or []))
        record_cursors(config, results)

        counters["notified"] = notified
        log_event(log, "notify.done", notified=notified)
        return {"notified": notified, "counters": counters}

    return notify


def alert_failure(config, exc: BaseException, run_id: str) -> None:
    """Any unhandled exception reaches a human.

    A silent failure on a 30-minute schedule is days of nothing before anyone
    notices, and the queue quietly going empty looks exactly like a slow week.
    """
    from wizcore.obs.log import traceback_tail

    try:
        send(
            f"🔴 <b>Lead Finder failed</b>\nrun_id: <code>{esc(run_id)}</code>\n\n"
            f"<pre>{esc(traceback_tail(exc))}</pre>",
            topic="alerts",
            dry_run=False,   # a real failure is a real failure in either mode
        )
    except Exception:
        log.error("could not send failure alert", exc_info=True)
