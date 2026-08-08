-- ═══════════════════════════════════════════════════════════════════════════
-- core 003 — rotating credentials, so nobody re-authorises by hand
-- ═══════════════════════════════════════════════════════════════════════════
-- THE PROBLEM THIS SOLVES
--
-- Three tokens in this system expire and all three are refreshable
-- indefinitely: Instagram (60d), Threads (60d), Pinterest (30d access + 60d
-- refresh). So in principle nothing ever needs a human.
--
-- In practice they did, because of one constraint: **a Modal container cannot
-- write its own secret.** The weekly cron could call refresh_access_token and
-- get a perfectly good new token, and then the container exited and the token
-- went with it. The next run loaded the same stale value from the secret. The
-- refresh was real; the persistence was not.
--
-- So rotating tokens move OUT of Modal secrets and into this table. A cron
-- refreshes and writes here; every agent reads here first and falls back to the
-- environment. Nothing needs a redeploy, and nothing needs a person.
--
-- WHAT DOES *NOT* BELONG HERE
--
-- Long-lived secrets that never rotate on a schedule — NEON_DATABASE_URL,
-- ANTHROPIC_API_KEY, BREVO_API_KEY, R2 keys. Those stay in Modal secrets, where
-- they are encrypted at rest by Cloudflare/Modal rather than sitting in a table
-- this system's own agents can read. The rule is narrow on purpose: **only
-- credentials that a machine rotates automatically live here.**
--
-- The bootstrap credential (NEON_DATABASE_URL) obviously cannot live in the
-- database it opens, which is the clearest statement of why this is a
-- supplement to Modal secrets and not a replacement for them.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS core.agent_credentials (
    name          text PRIMARY KEY,      -- INSTAGRAM_APP_ACCESS_TOKEN, ...
    value         text NOT NULL,
    -- Which agent's refresher owns this row. Two agents refreshing one token
    -- would race and invalidate each other's copy.
    owner_agent   text NOT NULL,
    expires_at    timestamptz,
    rotated_at    timestamptz NOT NULL DEFAULT now(),
    -- Rotation count, purely diagnostic: a token that has rotated 40 times is
    -- working, one stuck at 0 has never actually been refreshed and is about to
    -- become someone's Monday morning.
    rotations     int NOT NULL DEFAULT 0,
    last_error    text,
    notes         text
);

-- The watchdog's query: what is expiring soon, or has stopped rotating?
CREATE INDEX IF NOT EXISTS agent_credentials_expiry_idx
    ON core.agent_credentials (expires_at);

COMMENT ON TABLE core.agent_credentials IS
    'Machine-rotated tokens only (Instagram/Threads/Pinterest). Long-lived '
    'secrets stay in Modal secrets. Written by the owning agent''s refresh '
    'cron; read by every agent at startup, falling back to the environment.';
