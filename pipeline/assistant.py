"""The Telegram assistant: helps close a lead, and handles the few commands.

## Why this exists, and why it never posts anywhere

The founder works from a phone. A lead alert arrives with a ready reply; they
paste it on Reddit (or send the email) themselves. When the person answers,
the founder swipe-replies to that alert in Telegram, pastes what they said,
and this drafts the next message within seconds - grounded in the original
post and what was already sent, because Telegram includes the alert text as
`reply_to_message`.

It drafts; it never sends. Reddit closed new Data API access, ends the public
API by March 2027 and requires commercial deals and labelled bot accounts for
automation, and an automated reply bot is what gets accounts and whole domains
banned. A human pasting a message is the only zero-risk way to reply, so the
system's job is to make that paste take ten seconds.

## Why a webhook now

This is the one interactive surface, and it is a conversation: waiting for the
next 30-minute poll to answer "what do I say back?" defeats it. It also fixes a
real bug - nothing ever confirmed `getUpdates`, so every command was re-read by
every agent run for 24 hours and answered again each time. With a webhook set,
Telegram delivers each update exactly once, here, and `getUpdates` is off for
everyone (the agents' pollers treat that as "nothing to do").

Only messages from `TELEGRAM_CHAT_ID` are handled; the endpoint also checks
Telegram's secret-token header (see modal_app.telegram).
"""
from __future__ import annotations

import logging
import re

from wizcore.config import env_str
from wizcore.db.conn import connect
from wizcore.llm.client import LLMClient, extract_json
from wizcore.telegram.send import _post, esc

log = logging.getLogger("lead_finder.assistant")

HELP = (
    "<b>How I help</b>\n"
    "When someone answers you, <b>swipe-reply to their lead alert</b> here and "
    "paste what they said. I'll draft your next message in seconds.\n"
    "\n"
    "Commands:\n"
    "  <code>/done 42</code> - you sent a hand-off message (or posted a queued post)\n"
    "  <code>/replied 42</code> - a business replied; stop any follow-up\n"
    "  <code>/stop a@b.com</code> - never contact this address"
)

_LEAD_REF = re.compile(r"\blead #?(\d+)\b", re.I)


def allowed_chats() -> set[str]:
    return {c.strip() for c in env_str("TELEGRAM_CHAT_ID").split(",") if c.strip()}


def handle_update(config, update: dict) -> str:
    """Apply one Telegram update. Returns what was done, for the log. Never raises."""
    msg = update.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not chat_id or chat_id not in allowed_chats():
        return "ignored: not the owner's chat"
    if not text:
        return "ignored: no text"

    try:
        if text.startswith("/"):
            head, _, args = text.partition(" ")
            name = head.split("@", 1)[0].lstrip("/").lower()
            answer = _command(config, name, args.strip())
        else:
            answer = _draft_next(config, text, msg.get("reply_to_message") or {})
    except Exception as e:  # the owner must always get an answer
        log.exception("assistant failed")
        answer = f"⚠️ Something went wrong drafting that ({esc(type(e).__name__)}). Try again in a minute."

    _reply(chat_id, msg.get("message_id"), answer)
    return "answered"


# ── commands ──
def _command(config, name: str, args: str) -> str:
    if name in ("start", "help"):
        return HELP
    if name in ("done", "replied"):
        if not args.isdigit():
            return f"❓ <code>/{name}</code> needs a number, like <code>/{name} 42</code>"
        with connect(config.database_url) as conn:
            if name == "done":
                return _done(conn, int(args))
            return _replied(conn, int(args))
    if name == "stop":
        email = args.lower().strip()
        if "@" not in email:
            return "❓ <code>/stop</code> needs an email address"
        with connect(config.database_url) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO core.suppressions (channel, value_norm, reason) "
                "VALUES ('email', %s, 'manual') ON CONFLICT (channel, value_norm) DO NOTHING",
                (email,),
            )
            added = cur.rowcount > 0
        return f"🛑 {esc(email)} will never be contacted" if added else f"Already blocked: {esc(email)}"
    return HELP


def _done(conn, item_id: int) -> str:
    """Outreach hand-offs first, then the content queue - one /done for both."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.outreach_log SET status = 'marked_sent', sent_at = now() "
            "WHERE id = %s AND status = 'manual_pending' RETURNING entity_key",
            (item_id,),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE core.entities SET last_contacted_at = now() WHERE entity_key = %s",
                (row["entity_key"],),
            )
            return f"✅ {item_id} marked sent. Good luck!"
        cur.execute(
            "UPDATE content.manual_queue SET status = 'done', done_at = now() "
            "WHERE id = %s AND status = 'pending'",
            (item_id,),
        )
        if cur.rowcount > 0:
            cur.execute(
                "UPDATE content.social_posts SET status = 'published' "
                "WHERE external_post_id = %s AND status = 'queued'",
                (f"manual:{item_id}",),
            )
            return f"✅ post {item_id} marked posted"
    return f"❓ Nothing waiting with number {item_id} - maybe it is already marked"


def _replied(conn, log_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.outreach_log SET status = 'replied' "
            "WHERE id = %s RETURNING entity_key, lead_id",
            (log_id,),
        )
        row = cur.fetchone()
        if not row:
            return f"❓ No outreach message {log_id}"
        if row["lead_id"]:
            cur.execute(
                "UPDATE core.leads SET status = 'replied', updated_at = now() WHERE lead_id = %s",
                (row["lead_id"],),
            )
        cur.execute(
            "UPDATE core.entities SET last_contacted_at = now() WHERE entity_key = %s",
            (row["entity_key"],),
        )
    return (f"✅ {log_id} marked replied - no follow-up will go out. "
            "Swipe-reply to their message here with what they said and I'll draft your answer.")


# ── drafting the next message ──
def _draft_next(config, their_message: str, replied_to: dict) -> str:
    from scoring.prompts import CLOSE_SYSTEM

    context = (replied_to.get("text") or replied_to.get("caption") or "").strip()
    if context and not (replied_to.get("from") or {}).get("is_bot"):
        # They replied to their own message, not to an alert: use it as
        # extra context rather than pretending it is what we sent.
        context = "Earlier note from the founder: " + context

    facts = _facts(config)
    client = LLMClient(model=config.voice_model)
    raw = client.complete(
        system=CLOSE_SYSTEM + ("\n\nWizCodes facts you may rely on:\n" + facts if facts else ""),
        user=(
            ("THE LEAD ALERT (their original post or business, and what we sent):\n"
             f"{context[:3500]}\n\n" if context else
             "No alert context - the founder pasted this without replying to an alert.\n\n")
            + f"WHAT THEY JUST SAID:\n{their_message[:3000]}\n\n"
            "Draft the founder's next message."
        ),
        max_tokens=900,
        temperature=0.6,
    )
    parsed = extract_json(raw)
    if isinstance(parsed, dict) and str(parsed.get("reply") or "").strip():
        reply = str(parsed["reply"]).strip()
        tip = str(parsed.get("tip") or "").strip()
    else:
        reply, tip = (raw or "").strip(), ""
    if not reply:
        return "⚠️ I couldn't draft that one - try again, or paste a little more of what they said."

    if context:
        _mark_engaged(config, context)

    parts = ["<b>Send this next</b> - tap the box to copy", f"<pre>{esc(reply)}</pre>"]
    if tip:
        parts += ["", f"💡 {esc(tip)}"]
    if not context:
        parts += ["", "<i>Tip: swipe-reply to the lead alert next time, so I see what you sent them.</i>"]
    return "\n".join(parts)


def _mark_engaged(config, alert_text: str) -> None:
    """A lead whose person answered is a conversation now; the portal should say so."""
    m = _LEAD_REF.search(alert_text)
    if not m:
        return
    try:
        with connect(config.database_url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE core.leads SET status = 'replied', updated_at = now() "
                "WHERE lead_id = %s AND status IN ('new', 'notified', 'drafted', 'contacted')",
                (int(m.group(1)),),
            )
    except Exception:
        log.warning("could not mark lead %s replied", m.group(1), exc_info=True)


_FACTS_CACHE: dict[str, str] = {}


def _facts(config) -> str:
    """Site facts, cached for the life of the container - they change rarely."""
    if "block" in _FACTS_CACHE:
        return _FACTS_CACHE["block"]
    block = ""
    try:
        from wizcore.facts.site import SiteReader
        from wizcore.facts.snapshot import build_snapshot

        reader = SiteReader(repo=config.site_repo, token=config.site_read_token,
                            ref=config.site_branch, local_dir=config.site_local_dir or None)
        block = build_snapshot(reader).to_prompt_block(
            max_playbook_chars=2500, include_posts=False, include_playbook=False)
    except Exception:
        log.warning("facts unavailable; drafting without them", exc_info=True)
    _FACTS_CACHE["block"] = block
    return block


def _reply(chat_id: str, to_message_id, html_text: str) -> None:
    import json

    if len(html_text) > 4000:
        # Telegram's limit is 4096. A <pre> cut mid-block would 400, so close it.
        html_text = html_text[:3900].rsplit("\n", 1)[0]
        if html_text.count("<pre>") > html_text.count("</pre>"):
            html_text += "</pre>"
        html_text += "\n<i>(trimmed)</i>"
    # _post sends form data, so nested objects go as JSON strings.
    payload = {"chat_id": chat_id, "text": html_text, "parse_mode": "HTML",
               "link_preview_options": json.dumps({"is_disabled": True})}
    if to_message_id:
        payload["reply_parameters"] = json.dumps(
            {"message_id": to_message_id, "allow_sending_without_reply": True})
    try:
        _post("sendMessage", payload)
    except Exception:
        log.exception("could not send the assistant's answer")
