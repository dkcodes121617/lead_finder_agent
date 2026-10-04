-- Outreach claims only what it can actually act on, and the false "inbound"
-- labels come off the directory rows.
--
-- 1. claim_leads() used to hand Outreach every lead, and two kinds should never
--    have reached it:
--
--      * Website enquiries (source = 'inbound'). Someone who filled in the
--        contact form got a cold-pitch draft - the real 20 Aug grocery-app
--        enquiry was drafted "We recently built Noorly..." and the test row got
--        "your contact form is built with 'Other from t' - curious setup". An
--        enquiry is answered by a person, by email; the Lead Finder now alerts
--        it with a drafted reply instead.
--      * Social leads (Reddit, HN, Stack Exchange). They have no business
--        domain, so Outreach could not assess or email them and fell through to
--        a "linkedin" manual draft - 91 of them, none actionable, because a
--        Reddit username cannot be messaged on LinkedIn. They are already
--        alerted by the Lead Finder with a tap-to-copy reply for the thread,
--        which is the channel that works for them.
--
--    What is left is the population Outreach was built for: a business with a
--    real domain, which it can assess, enrich and email.
--
-- 2. From 13 Sep the classifier's presumed-lead branch gave directory listings
--    the inbound verdict - score 90, "inbound: self-identified on wizcodes.site".
--    The code is fixed; this removes the false score and label from the rows it
--    already wrote, so no report or queue ordering keeps trusting them.

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
          -- Enquiries are answered by a human, never pitched.
          AND l.source <> 'inbound'
          -- Only a business with a real domain can be assessed and emailed.
          AND e.domain IS NOT NULL
          AND e.domain <> ''
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

UPDATE core.leads
   SET intent_score = NULL,
       confidence   = 'low',
       reasoning    = 'directory prospect: no stated intent, qualified by the site '
                      'assessment [relabelled 2026-10-04: was stored as inbound]',
       updated_at   = now()
 WHERE source IN ('places', 'osm')
   AND reasoning = 'inbound: self-identified on wizcodes.site';
