-- ═══════════════════════════════════════════════════════════════════════════
-- WizCodes multi-agent system — core schema
-- ═══════════════════════════════════════════════════════════════════════════
-- Run once against the Neon project, then never edit this file: add 002_*.sql
-- and so on. Applied by wizcore.db.migrate under a Postgres advisory lock, so
-- two agents starting at the same minute cannot race each other.
--
-- WHY THIS FILE LIVES IN THE LEAD FINDER
--   Every agent folder is self-contained, so the shared `core` schema needs one
--   owner rather than four copies. The Lead Finder is it: the sole writer of
--   core.leads, the drain for core.inbound_events, and the origin of every
--   entity in the system. The other agents' migrations create only their own
--   private schema and assume `core` exists.
--
--   Consequence, and it is deliberate: run THIS agent's migrations first on a
--   fresh database. The other agents fail fast with a clear missing-relation
--   error rather than half-creating a schema they do not own.
--
-- DESIGN RULE — one writer per table.
--   The database is the message bus between agents. That only works if it is
--   obvious who owns what, so every table below names its writer. A reader
--   that needs a change asks the owner for a column; it never writes.
--
--   core.*      shared contract      writers noted per table
--   leadfind.*  Lead Finder only
--   lf_ckpt.*   LangGraph checkpointer — Lead Finder
--
--   content.* / cp_ckpt.*   -> content_poster_agent/migrations/
--   outreach.* / oa_ckpt.*  -> outreach_agent/migrations/
--
-- Checkpoint schemas are separate per agent on purpose. PostgresSaver keys rows
-- by thread_id alone; two agents sharing one schema would happily resume each
-- other's runs the moment a run_id ever collided.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS leadfind;
CREATE SCHEMA IF NOT EXISTS lf_ckpt;


-- ───────────────────────────────────────────────────────────────────────────
-- core.entities — one row per BUSINESS/PERSON, not per post.
-- Writer: Lead Finder (insert), Outreach Agent (contact bookkeeping columns).
--
-- This table is what stops the same company being pitched twice because it
-- turned up on Reddit on Monday and in Google Places on Thursday. `entity_key`
-- is the normalisation: registrable domain (eTLD+1) when a website is known,
-- else normalised email, else 'source:author'. Computed in one place —
-- wizcore.db.identity.entity_key() — never ad hoc in an agent.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core.entities (
    entity_key      text PRIMARY KEY,
    business_name   text,
    domain          text,
    country         text,
    region_tier     text NOT NULL DEFAULT 'other'
                    CHECK (region_tier IN ('us_eu_priority', 'other')),
    industry        text,
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_seen       timestamptz NOT NULL DEFAULT now(),
    lead_count      int NOT NULL DEFAULT 0,
    -- Set by a human, or by the reply/bounce handler. Checked before every
    -- draft AND again immediately before every send.
    do_not_contact  boolean NOT NULL DEFAULT false,
    dnc_reason      text,
    -- Last time ANY channel touched this entity. Drives the re-touch cooldown
    -- so a lead that resurfaces monthly does not get pitched monthly.
    last_contacted_at timestamptz,
    notes           text
);
CREATE INDEX IF NOT EXISTS entities_domain_idx ON core.entities (domain);
CREATE INDEX IF NOT EXISTS entities_last_contacted_idx ON core.entities (last_contacted_at);


-- ───────────────────────────────────────────────────────────────────────────
-- core.leads — the canonical lead queue. THE handoff point between agents.
-- Writer: Lead Finder (everything except the lifecycle columns).
-- Writer: Outreach Agent (status / claimed_* only, via the claim function).
--
-- Every lead source in the system lands here — Reddit, X, HN, Stack Exchange,
-- Google Places discovery, OSM, RSS, AND the site's own inbound (Wico AI +
-- contact form). One table, one dedup authority, one priority order.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core.leads (
    lead_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source          text NOT NULL,
    -- Stable id WITHIN a source, e.g. 'reddit:t3_1abc2de', 'wico:sess_9f3',
    -- 'places:ChIJN1t_tDeuEmsRUsoyG83frY4'. The UNIQUE below is what makes a
    -- re-scan of the same subreddit a no-op instead of a duplicate alert.
    source_uid      text NOT NULL,
    entity_key      text NOT NULL REFERENCES core.entities (entity_key) ON DELETE CASCADE,

    url             text,
    title           text,
    body_snippet    text,
    author          text,
    posted_at       timestamptz,
    discovered_at   timestamptz NOT NULL DEFAULT now(),

    -- ── classification (Lead Finder) ──
    is_lead         boolean,
    confidence      text CHECK (confidence IN ('low', 'medium', 'high')),
    service_line    text CHECK (service_line IN
                        ('Web Development', 'Mobile Apps', 'AI Automation', 'none')),
    -- 0-100. Inbound (Wico/contact form) is seeded high because someone who
    -- filled in a form on your own site outranks anyone found by scraping.
    intent_score    int CHECK (intent_score BETWEEN 0 AND 100),
    reasoning       text,
    reply_angle     text,

    -- ── lifecycle ──
    -- new         : classified, not yet handed to Outreach
    -- notified    : a Telegram alert went out (you may reply by hand)
    -- claimed     : Outreach has a lease on it (see claim_expires)
    -- drafted     : a message exists, waiting on approval
    -- contacted   : approved AND sent (or marked sent by hand)
    -- replied / won / lost : outcomes
    -- suppressed  : blocked by core.suppressions or do_not_contact
    -- expired     : aged out of the open queue without action
    status          text NOT NULL DEFAULT 'new',
    claimed_by      text,
    claimed_at      timestamptz,
    claim_expires   timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),

    raw             jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT leads_source_uid_uniq UNIQUE (source, source_uid),
    CONSTRAINT leads_status_ck CHECK (status IN
        ('new','notified','claimed','drafted','contacted',
         'replied','won','lost','suppressed','expired'))
);
-- The claim query's index: cheap even when the table is mostly dead rows.
CREATE INDEX IF NOT EXISTS leads_claimable_idx
    ON core.leads (intent_score DESC, discovered_at)
    WHERE status IN ('new', 'notified') AND is_lead;
CREATE INDEX IF NOT EXISTS leads_entity_idx ON core.leads (entity_key);
CREATE INDEX IF NOT EXISTS leads_reaper_idx ON core.leads (claim_expires)
    WHERE status = 'claimed';


-- ───────────────────────────────────────────────────────────────────────────
-- core.inbound_events — the landing table for the website's own leads.
-- Writer: the Cloudflare Workers (contact form + Wico AI), over Neon's HTTP
-- driver, using a dedicated `inbound_writer` role that holds INSERT on THIS
-- TABLE AND NOTHING ELSE.
--
-- Why a landing table instead of writing straight into core.leads: a public
-- internet-facing Worker holds this credential. Scoping it to one append-only
-- table means the worst case of that key leaking is junk rows in a staging
-- area, not a writable lead pipeline. It also preserves the single-writer rule
-- — the Lead Finder drains this table and remains the only thing that ever
-- inserts into core.leads.
--
--   CREATE ROLE inbound_writer LOGIN PASSWORD '...';
--   GRANT USAGE ON SCHEMA core TO inbound_writer;
--   GRANT INSERT ON core.inbound_events TO inbound_writer;
--   GRANT USAGE, SELECT ON SEQUENCE core.inbound_events_id_seq TO inbound_writer;
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core.inbound_events (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('contact_form','wico_lead','unsubscribe')),
    -- Idempotency: the Worker sends a client-generated id so a retried fetch
    -- cannot create a second lead for one form submission.
    event_uid    text NOT NULL,
    payload      jsonb NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    CONSTRAINT inbound_events_uid_uniq UNIQUE (kind, event_uid)
);
CREATE INDEX IF NOT EXISTS inbound_unprocessed_idx ON core.inbound_events (received_at)
    WHERE processed_at IS NULL;


-- ───────────────────────────────────────────────────────────────────────────
-- core.contacts — how to reach an entity. Writer: Outreach Agent (enrichment).
-- UNIQUE per (channel, value_norm) so the same inbox cannot be reached twice
-- under two different business names.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core.contacts (
    contact_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_key   text NOT NULL REFERENCES core.entities (entity_key) ON DELETE CASCADE,
    channel      text NOT NULL CHECK (channel IN ('email','linkedin','whatsapp','phone')),
    -- Normalised: email lowercased/trimmed, phone E.164, LinkedIn canonical URL.
    value_norm   text NOT NULL,
    display      text,
    -- 'scraped_contact_page' | 'pattern_guess' | 'places' | 'inbound_form' | 'wico'
    provenance   text NOT NULL,
    -- MX check at minimum before any pattern-guessed address is ever used.
    verified     boolean NOT NULL DEFAULT false,
    verified_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contacts_channel_value_uniq UNIQUE (channel, value_norm)
);


-- ───────────────────────────────────────────────────────────────────────────
-- core.suppressions — the do-not-contact list. Writer: anyone; read by all.
-- Unsubscribes, hard bounces, complaints, existing clients, personal contacts.
-- Checked TWICE by the Outreach Agent: before drafting (don't waste a call)
-- and again inside the send transaction (an unsubscribe may have landed while
-- the draft sat waiting for approval).
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core.suppressions (
    channel     text NOT NULL CHECK (channel IN ('email','linkedin','whatsapp','phone','domain')),
    value_norm  text NOT NULL,
    reason      text NOT NULL,   -- unsubscribed | bounced | complained | client | manual
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, value_norm)
);


-- ───────────────────────────────────────────────────────────────────────────
-- core.external_actions — the idempotency ledger. Writer: every agent.
--
-- Anything irreversible and outward-facing (publish a post, send an email)
-- claims a key here BEFORE the API call and records the result after. A retry
-- after a timeout finds the claim and refuses to fire twice. This is the
-- difference between "the network blipped" and "we posted the same thing to
-- Facebook three times".
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core.external_actions (
    idempotency_key text PRIMARY KEY,
    agent           text NOT NULL,
    kind            text NOT NULL,   -- publish_facebook | send_email | notify_telegram | ...
    target          text,            -- page id, email address, chat id
    requested_at    timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    ok              boolean,
    result          jsonb NOT NULL DEFAULT '{}'::jsonb
);


-- ───────────────────────────────────────────────────────────────────────────
-- core.outreach_log — one row per message per channel. Writer: Outreach Agent.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core.outreach_log (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lead_id         bigint REFERENCES core.leads (lead_id) ON DELETE SET NULL,
    entity_key      text NOT NULL REFERENCES core.entities (entity_key) ON DELETE CASCADE,
    contact_id      bigint REFERENCES core.contacts (contact_id) ON DELETE SET NULL,
    channel         text NOT NULL CHECK (channel IN ('email','linkedin','whatsapp')),
    sequence_step   int NOT NULL DEFAULT 1,   -- 1 = first touch, 2 = follow-up, ...
    subject         text,
    body            text NOT NULL,
    grounding_project text,                   -- the REAL WizCodes project cited
    weakness_cited  text,
    idempotency_key text UNIQUE REFERENCES core.external_actions (idempotency_key),
    status          text NOT NULL DEFAULT 'drafted' CHECK (status IN
                        ('drafted','approved','rejected','sent','manual_pending',
                         'marked_sent','bounced','replied','skipped')),
    approved_at     timestamptz,
    sent_at         timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outreach_log_entity_idx ON core.outreach_log (entity_key, sent_at DESC);
CREATE INDEX IF NOT EXISTS outreach_log_followup_idx ON core.outreach_log (status, sent_at)
    WHERE status IN ('sent', 'marked_sent');


-- ───────────────────────────────────────────────────────────────────────────
-- core.agent_runs — the shared run log. Writer: every agent, once per run.
-- Replaces per-repo .runs/history.log for the Modal agents, which have no repo
-- to commit a heartbeat into. Also the source for the daily Telegram digest.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core.agent_runs (
    run_id      uuid PRIMARY KEY,
    agent       text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running','ok','partial','aborted','failed')),
    counters    jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {"candidates":312,"new_leads":4}
    error       text
);
CREATE INDEX IF NOT EXISTS agent_runs_recent_idx ON core.agent_runs (agent, started_at DESC);


-- ───────────────────────────────────────────────────────────────────────────
-- core.approval_events — the durable record of every Telegram button press.
-- Writer: the agent_hub webhook router.
--
-- The router ALSO spawns the owning agent's resume function directly, so the
-- normal path is instant. This table is the backstop: if the spawn fails or
-- the agent was mid-deploy, a reconciliation cron replays unconsumed rows.
-- Approval is the one event in this system you cannot afford to drop.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core.approval_events (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent         text NOT NULL,          -- content_poster | outreach
    thread_id     text NOT NULL,          -- the LangGraph thread_id == run_id
    decision      text NOT NULL,          -- approve | edit | reject | mark_sent
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    telegram_update_id bigint UNIQUE,     -- Telegram redelivers on 5xx; this dedupes
    received_at   timestamptz NOT NULL DEFAULT now(),
    consumed_at   timestamptz
);
CREATE INDEX IF NOT EXISTS approval_pending_idx ON core.approval_events (agent, received_at)
    WHERE consumed_at IS NULL;


-- ───────────────────────────────────────────────────────────────────────────
-- core.credential_expiry — the token watchdog.
-- Meta long-lived tokens last ~60 days; LinkedIn access tokens ~60 days.
-- Both fail SILENTLY: publishing just stops working. A weekly cron reads this
-- table and Telegrams you 10 days out.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core.credential_expiry (
    name         text PRIMARY KEY,   -- META_PAGE_ACCESS_TOKEN | LINKEDIN_ACCESS_TOKEN | ...
    agent        text NOT NULL,
    expires_at   timestamptz,
    last_checked timestamptz,
    last_ok      boolean,
    notes        text
);


-- ───────────────────────────────────────────────────────────────────────────
-- core.spend_ledger — the budget guard.
-- Every metered call (LLM tokens, FLUX images, redditapis reads, Places calls,
-- Brevo sends) increments a row. Agents check their daily cap BEFORE the call
-- and hard-stop when it is hit. A runaway loop then costs one day's cap, not
-- one month's budget.
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core.spend_ledger (
    day          date NOT NULL,
    agent        text NOT NULL,
    provider     text NOT NULL,   -- claude_proxy | groq | huggingface | redditapis | places | brevo
    units        numeric NOT NULL DEFAULT 0,
    est_cost_usd numeric NOT NULL DEFAULT 0,
    PRIMARY KEY (day, agent, provider)
);


-- ═══════════════════════════════════════════════════════════════════════════
-- Per-agent private schemas
-- ═══════════════════════════════════════════════════════════════════════════

-- Lead Finder: where each source got to last time, so a run resumes instead of
-- re-scanning. Keeps both the API bill and the row churn down.
CREATE TABLE IF NOT EXISTS leadfind.source_cursors (
    source       text PRIMARY KEY,
    last_cursor  text,
    last_run_at  timestamptz,
    last_ok      boolean,
    last_error   text,
    -- Consecutive failures. The runner skips a source that has failed 5 times
    -- in a row and alerts once, instead of alerting every 30 minutes forever.
    fail_streak  int NOT NULL DEFAULT 0
);

-- content.* and outreach.* are NOT created here. They belong to the agents that
-- write them, in those agents' own migrations folders.


-- ═══════════════════════════════════════════════════════════════════════════
-- The handoff: claim leads for outreach.
-- ═══════════════════════════════════════════════════════════════════════════
-- SKIP LOCKED gives queue semantics without a queue service. Two Outreach runs
-- overlapping (a slow run plus the next cron) cannot take the same lead, and a
-- run that dies mid-flight releases its leads when the lease expires rather
-- than parking them forever.
--
--   SELECT * FROM core.claim_leads('outreach@run-uuid', 10, interval '30 min');
-- ═══════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION core.claim_leads(
    p_worker    text,
    p_limit     int,
    p_lease     interval DEFAULT interval '30 minutes'
) RETURNS SETOF core.leads
LANGUAGE sql AS $$
    WITH picked AS (
        SELECT l.lead_id
        FROM core.leads l
        JOIN core.entities e USING (entity_key)
        WHERE l.status IN ('new', 'notified')
          AND l.is_lead
          AND e.do_not_contact = false
          -- Re-touch cooldown: never pitch the same business inside 90 days,
          -- however many times it resurfaces across sources.
          AND (e.last_contacted_at IS NULL
               OR e.last_contacted_at < now() - interval '90 days')
        ORDER BY l.intent_score DESC NULLS LAST, l.discovered_at
        FOR UPDATE OF l SKIP LOCKED
        LIMIT p_limit
    )
    UPDATE core.leads l
       SET status        = 'claimed',
           claimed_by    = p_worker,
           claimed_at    = now(),
           claim_expires = now() + p_lease,
           updated_at    = now()
      FROM picked
     WHERE l.lead_id = picked.lead_id
    RETURNING l.*;
$$;

-- The reaper. Runs at the top of every Outreach run, before claiming.
-- Without it, one crashed run silently removes leads from circulation — the
-- "missed opportunity" failure mode, which is invisible precisely because
-- nothing errors.
CREATE OR REPLACE FUNCTION core.release_expired_claims() RETURNS int
LANGUAGE sql AS $$
    WITH released AS (
        UPDATE core.leads
           SET status = 'new', claimed_by = NULL, claimed_at = NULL,
               claim_expires = NULL, updated_at = now()
         WHERE status = 'claimed' AND claim_expires < now()
        RETURNING 1
    ) SELECT count(*)::int FROM released;
$$;

-- The open queue (§4.8 of the 2-agent doc): everything surfaced but not acted
-- on. Backs the /open Telegram command and the daily digest.
CREATE OR REPLACE VIEW core.open_queue AS
    SELECT l.lead_id, l.source, l.url, l.title, l.service_line, l.confidence,
           l.intent_score, l.reply_angle, l.discovered_at, e.business_name
      FROM core.leads l
      JOIN core.entities e USING (entity_key)
     WHERE l.status IN ('new', 'notified')
       AND l.is_lead
       AND l.discovered_at > now() - interval '14 days'
     ORDER BY l.intent_score DESC NULLS LAST, l.discovered_at DESC;
