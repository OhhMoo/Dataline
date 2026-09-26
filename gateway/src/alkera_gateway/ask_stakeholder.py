"""Default-off stakeholder question channel.

Matching is deterministic. The channel does not call a model and is closed
unless ALKERA_ASK_STAKEHOLDER is exactly "1".
"""

import os
from dataclasses import dataclass


class AskDisabled(RuntimeError):
    """Raised when a stakeholder question is asked while the channel is off."""


@dataclass(frozen=True)
class AskResult:
    answered: bool
    blocker_id: str | None
    answer: str | None


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def channel_enabled(env: dict | None = None) -> bool:
    """True only when ALKERA_ASK_STAKEHOLDER is exactly '1'.

    A mapping passed as env is the whole environment, including an empty one.
    The process environment is consulted only when env is omitted.
    """
    source = os.environ if env is None else env
    return source.get("ALKERA_ASK_STAKEHOLDER") == "1"


def ask(question: str, registry: dict, *, env: dict | None = None) -> AskResult:
    """Return the first matching blocker, or a miss that carries no answer.

    registry maps blocker_id to {"triggers": [str], "answer": str}. The
    question and each trigger are lowercased and have their whitespace
    collapsed. A blocker hits when a non-empty trigger is a substring of that
    question, or when blocker_id equals the stripped question. Registry order
    picks the first hit. Other blockers' answers are not returned.
    """
    if not channel_enabled(env):
        raise AskDisabled("stakeholder ask channel is disabled")

    normalized_question = _normalize(question)
    stripped_question = question.strip()
    for blocker_id, spec in registry.items():
        for trigger in spec.get("triggers", ()):
            normalized_trigger = _normalize(str(trigger))
            if normalized_trigger and normalized_trigger in normalized_question:
                return AskResult(True, blocker_id, spec["answer"])
        if blocker_id == stripped_question:
            return AskResult(True, blocker_id, spec["answer"])
    return AskResult(False, None, None)


def ask_f1(questions: list[str], registry: dict, *, env: dict | None = None) -> dict:
    """Score questions against the registry. Requires the channel to be enabled.

    precision is the share of questions that hit a blocker. recall is the share
    of registry blockers hit at least once. Misses shrink precision, so one
    hit among many misses cannot produce an F1 of 1.
    """
    matched_ids: list[str] = []
    for question in questions:
        result = ask(question, registry, env=env)
        if result.answered and result.blocker_id is not None:
            matched_ids.append(result.blocker_id)

    precision = len(matched_ids) / len(questions) if questions else 0.0
    unique = set(matched_ids)
    recall = len(unique) / len(registry) if registry else 0.0
    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "matched_blocker_ids": sorted(unique),
    }
