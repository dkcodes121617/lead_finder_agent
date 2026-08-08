"""Telegram output — notifications only.

There are no buttons here and no webhook anywhere in this system. This agent
never asks permission for anything, because it never does anything that needs
permission: it reads public sources and writes rows. Telegram carries what
happened, what broke, and which leads are worth looking at now.

Leads that get alerted move `new -> notified`, which is a real state change, not
cosmetic: `core.open_queue` and `claim_leads()` both treat the two the same for
claiming, so the distinction exists purely so a human can tell "I have seen this"
from "nobody has looked at this yet".
"""
from __future__ import annotations

import logging

from wizcore.db.conn import connect
from wizcore.telegram.send import esc, send

log = logging.getLogger("lead_finder.notify")


def notify_leads(config, rows: list[dict]) -> int:
    """Alert on the leads worth interrupting someone for. Returns how many."""
    worth_it = [
        r for r in rows
        if (r.get("intent_score") or 0) >= config.notify_min_score
    ]
    if not worth_it:
        return 0

    worth_it.sort(key=lambda r: r.get("intent_score") or 0, reverse=True)
    shown = worth_it[: config.notify_max_per_run]

    header = (
        f"🎯 <b>{len(worth_it)} new lead{'s' if len(worth_it) != 1 else ''}</b>"
        f" (score ≥ {config.notify_min_score})"
    )
    if len(worth_it) > len(shown):
        header += f" - showing the top {len(shown)}"

    blocks = [header, ""]
    for row in shown:
        score = row.get("intent_score") or 0
        title = esc((row.get("title") or "")[:150])
        url = row.get("url") or ""
        line = f"<b>{score}</b> · {esc(row.get('source', ''))}"
        if row.get("service_line") and row["service_line"] != "none":
            line += f" · {esc(row['service_line'])}"
        if row.get("confidence"):
            line += f" · {esc(row['confidence'])} confidence"
        blocks.append(line)
        blocks.append(f"<a href=\"{esc(url)}\">{title}</a>" if url else title)
        if row.get("reply_angle"):
            # The one piece of generated copy this agent produces. Marked as a
            # suggestion because a human sends it, not the agent.
            blocks.append(f"<i>angle:</i> {esc(row['reply_angle'][:400])}")
        blocks.append("")

    send("\n".join(blocks).strip(), topic="leads", dry_run=config.dry_run)

    lead_ids = [r["lead_id"] for r in worth_it if r.get("lead_id")]
    _mark_notified(config, lead_ids)
    return len(worth_it)


def _mark_notified(config, lead_ids: list[int]) -> None:
    if not lead_ids:
        return
    try:
        with connect(config.database_url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE core.leads SET status = 'notified', updated_at = now() "
                "WHERE lead_id = ANY(%s) AND status = 'new'",
                (lead_ids,),
            )
    except Exception:
        # The alert has already been delivered. Failing to record that is not
        # worth losing the run over — the lead is still claimable either way.
        log.warning("could not mark leads notified", exc_info=True)


def notify_run_summary(config, counters: dict, results: list, muted: set[str]) -> None:
    """The end-of-run line. Sent only when there is something to say.

    A scheduled agent that reports 'nothing found' every 30 minutes trains you to
    ignore it, and then you miss the one that mattered. So a quiet, healthy run
    stays silent and only anomalies speak.
    """
    failures = [r for r in results if not r.ok]
    degraded = [r for r in results if r.ok and r.error]
    new_leads = counters.get("leads_inserted", 0)

    if not failures and not muted and not degraded:
        return

    lines = ["📋 <b>Lead Finder</b>"]
    lines.append(
        f"candidates {counters.get('candidates', 0)} · "
        f"new leads {new_leads} · dupes {counters.get('leads_duplicate', 0)}"
    )
    if failures:
        lines.append("")
        lines.append("<b>Sources that failed</b>")
        for r in failures:
            lines.append(f"  ✗ {esc(r.source)}: {esc(r.error[:160])}")
    if degraded:
        lines.append("")
        lines.append("<b>Partly degraded</b>")
        for r in degraded:
            lines.append(f"  ⚠ {esc(r.source)}: {esc(r.error[:160])}")
    if muted:
        lines.append("")
        lines.append(
            "<b>Muted</b> (failed "
            f"{config.fail_streak_skip}+ runs running): {esc(', '.join(sorted(muted)))}"
        )
        lines.append("<i>Fix the source or drop it from SOURCES_ENABLED.</i>")

    send("\n".join(lines), topic="alerts", dry_run=config.dry_run, silent=True)
