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

    # ── One message per lead, not one digest of all of them ──
    #
    # A lead alert exists to get a reply written by a human onto a stranger's
    # thread, on a phone, in the two minutes before the thread goes cold. That
    # makes the message a TOOL, not a report, and it has to survive being read
    # with one thumb:
    #
    #   * the reply text goes in <pre>, because Telegram renders that as a
    #     tap-to-copy block on mobile. Italic inline text - what this used to
    #     send - has to be selected by hand, which on a phone means dragging
    #     two handles over a paragraph and usually missing the last word.
    #   * it is NOT truncated any more. The old 400-char clip cut the end off
    #     the longest angles, which is exactly where the ask lives, so the one
    #     part you cannot write yourself was the part that got dropped.
    #   * the link is its own line with a verb on it, not wrapped around the
    #     post title, so the thing to tap is obvious.
    #
    # Digesting several leads into one message undid all of that: chunking split
    # <pre> blocks across message boundaries, and copying one reply out of five
    # meant selecting inside a wall of text. Volume is not a concern here the way
    # it is for run summaries - this fires only above NOTIFY_MIN_SCORE, which is
    # roughly one message a day, and it is the one message worth opening.
    for row in shown:
        score = row.get("intent_score") or 0
        title = esc((row.get("title") or "")[:200])
        url = row.get("url") or ""

        meta = f"<b>{score}</b> · {esc(row.get('source', ''))}"
        if row.get("service_line") and row["service_line"] != "none":
            meta += f" · {esc(row['service_line'])}"
        if row.get("confidence"):
            meta += f" · {esc(row['confidence'])} confidence"

        parts = ["🎯 <b>Lead needs your reply</b>", "", meta, title, ""]
        if url:
            parts.append(f'👉 <a href="{esc(url)}">Open the thread and reply</a>')
            parts.append("")
        if row.get("reply_angle"):
            # Written by the agent, sent by a human. Tap and hold to copy.
            parts.append("<b>Send this:</b>")
            parts.append(f"<pre>{esc(row['reply_angle'])}</pre>")
        else:
            parts.append("<i>No draft reply - read the thread and write one.</i>")

        send("\n".join(parts).strip(), topic="leads", dry_run=config.dry_run)

    if len(worth_it) > len(shown):
        send(
            f"…and {len(worth_it) - len(shown)} more lead(s) above "
            f"score {config.notify_min_score} this run. See the portal.",
            topic="leads", dry_run=config.dry_run, silent=True,
        )

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
    """Speak only when a source CHANGES state. Never on steady state.

    The previous version sent whenever anything was failing or muted. This agent
    runs 48 times a day, and from 26 Aug at least one source failed on every
    single run, so it sent ~48 messages a day saying the same three things —
    about 1,400 in three weeks. The cost of that is not noise, it is that the
    Pinterest-token warning and the site-deploy failure landed in a channel
    nobody could still read.

    So: a source that starts failing says so once. A source that recovers says so
    once. A source that has been failing for nineteen days says nothing at all,
    because there is nothing new for a human to do about it that they were not
    already told. Steady-state health lives in the portal, which reads the same
    `leadfind.source_cursors` row this is derived from.

    `prior` is read before `record_cursors` runs, so it still holds the previous
    attempt's verdict — that ordering is what makes the comparison possible and
    is why `record_cursors` is called after this in the graph.
    """
    from pipeline.persist import prior_source_state

    prior = prior_source_state(config)
    # A source that was never attempted this run carries no verdict; comparing it
    # would report a recovery that did not happen.
    attempted = [r for r in results if not getattr(r, "skipped", False)]

    newly_broken = [r for r in attempted if not r.ok and prior.get(r.source, True)]
    recovered = [r for r in attempted if r.ok and not prior.get(r.source, True)]

    if not newly_broken and not recovered:
        return

    lines = ["📋 <b>Lead Finder</b>"]
    if newly_broken:
        lines.append("")
        lines.append("<b>Started failing</b>")
        for r in newly_broken:
            lines.append(f"  ✗ {esc(r.source)}: {esc(r.error[:200])}")
        # The one failure mode that is fixed with a credit card rather than code,
        # and the one that killed 85% of lead flow for nineteen days unnoticed.
        if any("402" in (r.error or "") or "credit" in (r.error or "").lower()
               for r in newly_broken):
            lines.append("")
            lines.append("💳 <b>Out of vendor credits</b> — top up to restore this source.")
    if recovered:
        lines.append("")
        lines.append("<b>Recovered</b>")
        for r in recovered:
            lines.append(f"  ✓ {esc(r.source)}")

    send("\n".join(lines), topic="alerts", dry_run=config.dry_run, silent=True)
