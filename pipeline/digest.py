"""The daily digest — one message that answers "is this system working?".

It lives in the Lead Finder because that agent owns the `core` schema and runs
most often, but it reports on all five agents. Nothing here writes; it is a
read-only view across the message bus.

## What it reports, and why each line earns its place

Every item is a **silent** failure — something that goes wrong without anything
erroring, which is the only class of problem a digest is worth sending for:

  - **runs by agent** — an agent that simply stopped running looks identical to
    a quiet week
  - **stuck runs** — a container killed mid-flight leaves a row in `running`
    forever and nothing else notices
  - **open claims** — an idempotency claim with no outcome blocks that action
    permanently, and blocks it quietly
  - **open queue depth** — leads surfaced and never worked
  - **manual queues** — X threads and outreach messages handed over and never
    posted; these are the ones that evaporate
  - **bounce / complaint rates** — the two numbers that decide whether the email
    channel exists in three months
  - **credential expiry** — Meta and Instagram tokens fail silently; publishing
    just stops

A healthy day still sends, unlike the per-run notifications. That is deliberate:
a digest you only receive when something is wrong is one you cannot distinguish
from a digest that failed to send.
"""
from __future__ import annotations

import logging

from wizcore.db.conn import connect, fetch_all, fetch_one
from wizcore.telegram.send import esc, send

log = logging.getLogger("lead_finder.digest")


def build(config) -> str:
    """Render the digest. Returns the message body."""
    lines = ["📊 <b>WizCodes daily digest</b>", ""]
    with connect(config.database_url, autocommit=True) as conn:
        lines += _runs(conn)
        lines += _leads(conn)
        lines += _manual(conn)
        lines += _email_health(conn, config)
        lines += _stuck(conn)
        lines += _credentials(conn)
    return "\n".join(lines).strip()


def _runs(conn) -> list[str]:
    rows = fetch_all(
        conn,
        "SELECT agent, status, count(*) AS n FROM core.agent_runs "
        "WHERE started_at > now() - interval '24 hours' "
        "GROUP BY agent, status ORDER BY agent, status",
    )
    if not rows:
        return ["<b>Runs (24h)</b>", "  ⚠️ no agent ran in the last 24 hours", ""]

    by_agent: dict[str, list[str]] = {}
    for row in rows:
        by_agent.setdefault(row["agent"], []).append(f"{row['n']} {row['status']}")
    out = ["<b>Runs (24h)</b>"]
    for agent, parts in by_agent.items():
        bad = any(s in " ".join(parts) for s in ("failed", "aborted"))
        out.append(f"  {'❌' if bad else '✅'} {esc(agent)}: {esc(', '.join(parts))}")
    return [*out, ""]


def _leads(conn) -> list[str]:
    new = fetch_one(
        conn,
        "SELECT count(*) AS n FROM core.leads "
        "WHERE discovered_at > now() - interval '24 hours'",
    )
    queue = fetch_one(conn, "SELECT count(*) AS n FROM core.open_queue")
    by_source = fetch_all(
        conn,
        "SELECT source, count(*) AS n FROM core.leads "
        "WHERE discovered_at > now() - interval '24 hours' AND is_lead "
        "GROUP BY source ORDER BY n DESC LIMIT 6",
    )
    out = [
        "<b>Leads</b>",
        f"  {(new or {}).get('n', 0)} new in 24h · open queue {(queue or {}).get('n', 0)}",
    ]
    if by_source:
        out.append("  " + esc(", ".join(f"{r['source']} {r['n']}" for r in by_source)))
    stale = fetch_one(
        conn,
        "SELECT count(*) AS n FROM core.leads WHERE status IN ('new','notified') "
        "AND is_lead AND discovered_at < now() - interval '14 days'",
    )
    if (stale or {}).get("n"):
        out.append(f"  ⚠️ {stale['n']} lead(s) older than 14 days never worked")
    return [*out, ""]


def _manual(conn) -> list[str]:
    out: list[str] = []
    outreach = fetch_all(
        conn,
        "SELECT id, entity_key, channel, created_at FROM core.outreach_log "
        "WHERE status = 'manual_pending' ORDER BY created_at LIMIT 10",
    )
    content = []
    try:
        content = fetch_all(
            conn,
            "SELECT id, platform, pillar, created_at FROM content.manual_queue "
            "WHERE status = 'pending' ORDER BY created_at LIMIT 10",
        )
    except Exception:
        # The table arrives with content_poster migration 003; a digest that
        # crashes because one agent has not migrated yet is worse than a digest
        # missing one section.
        log.debug("content.manual_queue not available", exc_info=True)

    if not (outreach or content):
        return out
    out.append("<b>Waiting on you</b>")
    for row in content:
        out.append(
            f"  {esc(row['platform'])} ({esc(row['pillar'] or '')}) - "
            f"<code>/done {row['id']}</code>"
        )
    for row in outreach:
        out.append(
            f"  {esc(row['channel'])} to {esc(row['entity_key'])} - "
            f"<code>/done {row['id']}</code>"
        )
    return [*out, ""]


def _email_health(conn, config) -> list[str]:
    row = fetch_one(
        conn,
        "SELECT count(*) FILTER (WHERE status IN ('sent','marked_sent')) AS sent, "
        "       count(*) FILTER (WHERE status = 'bounced') AS bounced, "
        "       count(*) FILTER (WHERE status = 'replied') AS replied "
        "FROM core.outreach_log "
        "WHERE channel = 'email' AND created_at > now() - interval '14 days'",
    ) or {}
    sent = int(row.get("sent") or 0)
    if not sent:
        return []
    bounced = int(row.get("bounced") or 0)
    complaints = fetch_one(
        conn,
        "SELECT count(*) AS n FROM core.suppressions "
        "WHERE reason = 'complained' AND created_at > now() - interval '14 days'",
    ) or {}
    complaint_n = int(complaints.get("n") or 0)
    bounce_rate = bounced / sent
    complaint_rate = complaint_n / sent
    # These two go at the top of the section because they are the numbers that
    # end a sending domain, and a rate creeping upward is the warning.
    flag = "🛑" if (bounce_rate > 0.03 or complaint_rate > 0.001) else "✅"
    return [
        "<b>Email (14d)</b>",
        (
            f"  {flag} {sent} sent · {row.get('replied', 0)} replied · "
            f"bounce {bounce_rate:.1%} · complaints {complaint_rate:.2%}"
        ),
        "",
    ]


def _stuck(conn) -> list[str]:
    out: list[str] = []
    runs = fetch_all(
        conn,
        "SELECT agent, run_id, started_at FROM core.agent_runs "
        "WHERE status = 'running' AND started_at < now() - interval '90 minutes' LIMIT 5",
    )
    claims = fetch_all(
        conn,
        "SELECT agent, kind, requested_at FROM core.external_actions "
        "WHERE completed_at IS NULL AND requested_at < now() - interval '2 hours' LIMIT 5",
    )
    if runs:
        out.append("<b>Stuck runs</b>")
        out += [f"  ⚠️ {esc(r['agent'])} since {r['started_at']:%d %b %H:%M}" for r in runs]
    if claims:
        out.append("<b>Blocked actions</b>")
        # An open claim refuses every future attempt at that action. It is the
        # quietest failure in the system, so it is named explicitly.
        out += [
            f"  ⚠️ {esc(c['agent'])}/{esc(c['kind'])} claimed "
            f"{c['requested_at']:%d %b %H:%M}, never completed"
            for c in claims
        ]
    return [*out, ""] if out else out


def _credentials(conn) -> list[str]:
    rows = fetch_all(
        conn,
        "SELECT name, agent, expires_at, last_ok FROM core.credential_expiry "
        "WHERE expires_at IS NOT NULL "
        "  AND expires_at < now() + interval '10 days' "
        "ORDER BY expires_at",
    )
    failing = fetch_all(
        conn,
        "SELECT name, agent FROM core.credential_expiry WHERE last_ok = false",
    )
    out: list[str] = []
    if rows or failing:
        out.append("<b>Credentials</b>")
    for row in rows:
        out.append(
            f"  ⏳ {esc(row['name'])} ({esc(row['agent'])}) expires "
            f"{row['expires_at']:%d %b}"
        )
    for row in failing:
        out.append(f"  ❌ {esc(row['name'])} ({esc(row['agent'])}) last check FAILED")
    return [*out, ""] if out else out


def send_digest(config) -> bool:
    """Build and send. Never raises — a digest is reporting, not work."""
    try:
        body = build(config)
    except Exception:
        log.error("could not build digest", exc_info=True)
        return False
    # Sent even in DRY_RUN: this is a report about the system, not an outward
    # action, and a dry-run week with no digest tells you nothing.
    return send(body, topic="alerts", dry_run=False, silent=True)
