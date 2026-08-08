"""Classification — Groq primary, the Claude proxy as fallback.

## Why Groq and not the proxy

This is the one high-volume, low-stakes LLM job in the system: hundreds of
candidates a day, and the output is a boolean, a label and one sentence. Groq's
free tier covers it; the proxy is rate-limited and latency-spiky (30-70s on long
calls, with intermittent 502 spells); and spending proxy budget on triage starves
the jobs where wording actually decides the outcome.

The model choice was measured rather than argued. At 300 candidates a run only a
fast model is viable — in the bench, haiku answered in 4.2s where opus-5 took
10.6s, and every model agreed on the labels. So the fallback is haiku, and the
proxy's strongest model is reserved for the reply-angle draft on high-confidence
leads only, which is the one place here where wording matters.

## Everything degrades rather than fails

If both providers are down, candidates come back **unclassified** rather than
dropped, and the run persists them with `is_lead = NULL`. A lead that could not
be scored is still a lead; discarding it because a vendor was down is the silent
data loss this system is built to avoid.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from wizcore.llm.client import LLMClient, extract_json

from scoring.prompts import CLASSIFY_SYSTEM, REPLY_ANGLE_SYSTEM, classify_user_prompt

log = logging.getLogger("lead_finder.scoring")

VALID_SERVICE_LINES = {"Web Development", "Mobile Apps", "AI Automation", "none"}
VALID_CONFIDENCE = {"low", "medium", "high"}


@dataclass
class Verdict:
    """One classification result, already coerced to what the schema accepts.

    A dataclass, and that is not a style choice. Verdicts live in graph state,
    and LangGraph's checkpointer serialises state with msgpack — which knows how
    to encode a dataclass and raises `Type is not msgpack serializable` on a
    plain class with `__slots__`. Every other object that crosses state
    (`Candidate`, `SourceResult`) is a dataclass for the same reason.
    """

    is_lead: bool | None = None
    confidence: str | None = None
    service_line: str | None = None
    intent_score: int | None = None
    reasoning: str = ""
    provider: str = ""

    @classmethod
    def unclassified(cls, why: str) -> Verdict:
        return cls(reasoning=why[:300], provider="none")


def _coerce(raw: dict, provider: str) -> Verdict:
    """Force a model's answer into the shape `core.leads` will accept.

    Everything here is defensive on purpose: these values go straight into CHECK
    constraints, and a rejected INSERT at the end of a run throws away every
    candidate the run collected, not just the malformed one.
    """
    service = str(raw.get("service_line") or "none").strip()
    if service not in VALID_SERVICE_LINES:
        # Models reliably return the right idea with the wrong wording
        # ("Web Dev", "web development"). Match case-insensitively before
        # giving up, rather than discarding a correct judgement over casing.
        match = next(
            (v for v in VALID_SERVICE_LINES if v.lower() == service.lower()), None
        )
        service = match or "none"

    confidence = str(raw.get("confidence") or "").strip().lower()
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"

    try:
        score = int(float(raw.get("intent_score", 0)))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    is_lead = bool(raw.get("is_lead"))
    if not is_lead:
        # Keeping a service line on a non-lead would let it be queried as though
        # it were one.
        service = "none"

    return Verdict(
        is_lead=is_lead,
        confidence=confidence,
        service_line=service,
        intent_score=score,
        reasoning=str(raw.get("reasoning") or "")[:600],
        provider=provider,
    )


class Classifier:
    def __init__(self, config, budget=None):
        self.config = config
        self.budget = budget
        self._groq = None
        self._llm: LLMClient | None = None

    # ── providers ──
    def _groq_client(self):
        if self._groq is None:
            from groq import Groq

            self._groq = Groq(api_key=self.config.groq_api_key)
        return self._groq

    def _proxy(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(
                model=self.config.fallback_classify_model,
                on_usage=self.budget.on_llm_usage() if self.budget else None,
            )
        return self._llm

    # ── public ──
    def classify(self, candidates: list) -> list[Verdict]:
        """One verdict per candidate, always the same length as the input."""
        verdicts: list[Verdict] = [Verdict.unclassified("not attempted")] * len(candidates)
        size = max(1, self.config.classify_batch_size)

        for start in range(0, len(candidates), size):
            batch = candidates[start : start + size]
            items = [
                {
                    "i": start + offset,
                    "source": c.source,
                    "title": c.title[:200],
                    "text": c.text_for_classifier(),
                }
                for offset, c in enumerate(batch)
            ]
            for verdict, index in self._classify_batch(items):
                if 0 <= index < len(verdicts):
                    verdicts[index] = verdict

        return verdicts

    def _classify_batch(self, items: list[dict]) -> list[tuple[Verdict, int]]:
        prompt = classify_user_prompt(items)
        order = [item["i"] for item in items]

        groq_available = (
            self.config.classifier_provider == "groq"
            and bool(self.config.groq_api_key)
            and (self.budget is None or self.budget.afford("groq", 1))
        )
        if groq_available:
            try:
                return self._parse(self._call_groq(prompt), order, "groq")
            except Exception as e:
                log.warning("groq classify failed, falling back: %s", e)

        try:
            return self._parse(self._call_proxy(prompt), order, "claude_proxy")
        except Exception as e:
            log.error("classification failed on both providers: %s", e)
            return [(Verdict.unclassified(f"both providers failed: {e}"), i) for i in order]

    def _call_groq(self, prompt: str) -> str:
        resp = self._groq_client().chat.completions.create(
            model=self.config.groq_classify_model,
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,   # a label, not prose
            max_tokens=4000,
        )
        return resp.choices[0].message.content or ""

    def _call_proxy(self, prompt: str) -> str:
        return self._proxy().complete(
            system=CLASSIFY_SYSTEM,
            user=prompt,
            max_tokens=4000,
            temperature=0.1,
            model=self.config.fallback_classify_model,
        )

    def _parse(self, raw: str, order: list[int], provider: str) -> list[tuple[Verdict, int]]:
        parsed = extract_json(raw)
        if isinstance(parsed, dict):
            # A single-item batch sometimes comes back unwrapped.
            parsed = [parsed]
        if not isinstance(parsed, list):
            raise ValueError(f"expected a JSON array, got {type(parsed).__name__}")

        out: list[tuple[Verdict, int]] = []
        for position, entry in enumerate(parsed):
            if not isinstance(entry, dict):
                continue
            # Trust the model's own index when it echoed one back, and fall back
            # to position. Batches do occasionally come back reordered, and
            # position-only mapping would then attach every verdict to the wrong
            # candidate — wrong in a way that looks completely normal.
            index = entry.get("i")
            if not isinstance(index, int) or index not in order:
                index = order[position] if position < len(order) else None
            if index is None:
                continue
            out.append((_coerce(entry, provider), index))

        missing = set(order) - {i for _, i in out}
        for index in missing:
            out.append((Verdict.unclassified("model omitted this item"), index))
        return out

    # ── reply angle, high-confidence leads only ──
    def reply_angle(self, candidate, verdict: Verdict, facts_block: str) -> str:
        """One or two sentences to open a reply with. '' when it is not worth it.

        Gated on score because this is the only call here that uses the
        expensive model, and a reply angle for a lead nobody will contact is
        budget spent on nothing.
        """
        if (verdict.intent_score or 0) < self.config.reply_angle_min_score:
            return ""
        if self.budget and not self.budget.afford("claude_proxy", 1):
            return ""
        try:
            client = LLMClient(
                model=self.config.voice_model,
                on_usage=self.budget.on_llm_usage() if self.budget else None,
            )
            text = client.complete(
                system=REPLY_ANGLE_SYSTEM + "\n\nWizCodes facts you may rely on:\n" + facts_block,
                user=(
                    f"This person posted on {candidate.source}:\n\n"
                    f"title: {candidate.title}\n"
                    f"text: {candidate.text_for_classifier(800)}\n\n"
                    "Write the opening sentences of a reply to them."
                ),
                max_tokens=200,
                temperature=0.7,
            )
            return text.strip()[:600]
        except Exception as e:
            log.warning("reply angle failed: %s", e)
            return ""


def verdicts_summary(verdicts: list[Verdict]) -> dict:
    """Counters for the run row and the digest."""
    leads = [v for v in verdicts if v.is_lead]
    return {
        "classified": sum(1 for v in verdicts if v.is_lead is not None),
        "unclassified": sum(1 for v in verdicts if v.is_lead is None),
        "leads": len(leads),
        "high_confidence": sum(1 for v in leads if v.confidence == "high"),
        "by_provider": json.dumps(
            {p: sum(1 for v in verdicts if v.provider == p) for p in {v.provider for v in verdicts}}
        ),
    }
