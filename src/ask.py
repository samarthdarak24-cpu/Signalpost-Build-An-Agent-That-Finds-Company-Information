"""Ask Signalpost: answer questions from verified facts, and only from them.

This is deliberately *not* a researcher. It never searches, never crawls and
never calls a model to go and find something. It routes a question to the facts
already on a profile and answers from those, citing the source and the quote for
each sentence. When the profile holds nothing that answers the question, it says
so -- it does not fall back to a general model, because a general model's answer
is exactly the kind of unsourced claim this whole project exists to avoid.

The routing is keyword-based and transparent: a question maps to one of the five
synthesis views, and the answer is that view's evidence-backed lines. It is a
small, honest interface over a correct knowledge base, which is a far safer
thing to expose than a free-roaming chatbot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from profiles import Profile
from synthesis import SECTION_LABELS, synthesis

__all__ = ["Answer", "INTENTS", "ask"]

#: intent -> (keywords, synthesis section). First match wins, so order matters:
#: the more specific intents are listed before the catch-all brief.
INTENTS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("leadership", ("who runs", "who is the ceo", "ceo", "chief exec", "managing director",
                    "board", "chair", "leder", "styre", "daglig leder"), "who_runs_it"),
    ("financials", ("financial", "revenue", "result", "turnover", "profit", "assets",
                    "equity", "debt", "accounts", "omsetning", "regnskap"), "latest_financials"),
    ("hiring", ("hiring", "jobs", "job", "vacanc", "open role", "careers", "stilling",
                "jobb", "ansatt"), "working_here"),
    ("activity", ("changed", "recent", "update", "news", "activity", "announcement",
                  "nyheter", "aktivitet"), "recent_activity"),
    ("location", ("where", "address", "location", "located", "office", "workplace",
                  "adresse", "hvor"), "company_brief"),
    ("about", ("what does", "what do", "do they do", "about", "describe", "industry",
               "hva gjør", "bransje"), "company_brief"),
)

_DEFAULT_SECTION = "company_brief"


@dataclass(slots=True)
class Answer:
    """A question, the intent it resolved to, and the evidence behind the reply."""

    question: str
    intent: str
    section: str
    text: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    answered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "intent": self.intent,
            "section": self.section,
            "section_label": SECTION_LABELS.get(self.section, self.section),
            "answered": self.answered,
            "text": self.text,
            "evidence": self.evidence,
        }


def _classify(question: str) -> tuple[str, str]:
    q = question.casefold()
    for intent, keywords, section in INTENTS:
        if any(k in q for k in keywords):
            return intent, section
    return "brief", _DEFAULT_SECTION


def ask(profile: Profile, question: str) -> Answer:
    """Answer ``question`` strictly from ``profile``'s published facts."""
    intent, section = _classify(question)
    synth = synthesis(profile)
    view = synth["views"][section]
    lines = view["lines"]

    if not lines:
        return Answer(
            question=question, intent=intent, section=section,
            text=("That information is not available in this profile. No verified "
                  "source produced it during this run."),
            evidence=[], answered=False,
        )

    # Build a plain sentence from the evidence-backed lines, citing the source.
    parts = [f"{line['label']}: {line['value']}" for line in lines]
    text = "; ".join(parts) + "."
    return Answer(
        question=question, intent=intent, section=section,
        text=text, evidence=lines, answered=True,
    )
