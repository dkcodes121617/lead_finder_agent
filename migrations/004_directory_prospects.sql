-- Let already-discovered directory prospects reach the assessment that judges them.
--
-- `sources/places.py` and `sources/osm.py` now set `presumed_lead`, so NEW rows
-- skip the intent classifier and are qualified by the Outreach agent's weakness
-- assessment instead — which is what `seed_score=None` on those sources always
-- meant by "it has to earn its score from the assessment".
--
-- That fixes discovery going forward and does nothing for the 312 businesses
-- already found, because `UNIQUE (source, source_uid)` means a re-scan is a
-- no-op: they will never be re-inserted, so without this they stay
-- `is_lead = false` permanently. Places is a metered source — 1,780 API calls
-- have already been paid for these rows.
--
-- Only rows that still have somewhere to go are touched:
--   * status 'new'      — nothing claimed, contacted or suppressed is disturbed
--   * a real domain     — no website means nothing to assess
--   * is_lead = false   — the classifier's verdict is only overridden where it
--                        was the wrong judge, never where it said yes
--
-- `intent_score` is deliberately left NULL. core.claim_leads() orders by it
-- DESC NULLS LAST, so these queue strictly behind anything that stated real
-- intent, and they sit below NOTIFY_MIN_SCORE so they never raise an alert.
-- Whether any of them is actually contacted is still decided downstream by
-- MIN_WEAKNESS_SCORE, the 90-day entity cooldown, MX verification and the
-- sending warm-up.

UPDATE core.leads l
   SET is_lead    = true,
       reasoning  = coalesce(nullif(l.reasoning, ''), '') ||
                    ' [requalified: directory prospect, judged by site assessment]',
       updated_at = now()
  FROM core.entities e
 WHERE e.entity_key = l.entity_key
   AND l.source IN ('places', 'osm')
   AND l.status = 'new'
   AND l.is_lead = false
   AND e.domain IS NOT NULL
   AND e.domain <> ''
   AND e.do_not_contact = false;
