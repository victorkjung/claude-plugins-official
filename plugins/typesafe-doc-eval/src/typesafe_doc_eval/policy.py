"""Code-owned policy: turn typed answers into a composite score and a status.

The model supplies judgments; this module decides. Thresholds live in the rubric's `policy`
block, so changing them re-scores nothing: the same answers produce a new status.

    pass    verdict approve (if the rubric has one) and composite >= pass_at
    revise  fixable: verdict revise, or composite between fail_below and pass_at
    review  a person should decide: conflicting signals, a low-confidence verdict (unless the
            composite fails on its own), a flagged answer, a truncated document, or a document
            that may not fit the rubric
    fail    verdict reject, or composite < fail_below
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from typesafe_sdk import Answer, ChoiceAnswer, NoulAnswer, ScoreAnswer

from typesafe_doc_eval.rubric import (
    KIND_CHECK_ID,
    ChoiceDimension,
    NoulDimension,
    Policy,
    Rubric,
    ScoreDimension,
)

Status = Literal["pass", "revise", "review", "fail"]
SEVERITY: dict[str, int] = {"pass": 0, "revise": 1, "review": 2, "fail": 3}
VERDICT_STATUS: dict[str, Status] = {"approve": "pass", "revise": "revise", "reject": "fail"}
WEAK_BELOW = 0.75
"""Weighted dimensions scoring under this share of their scale are listed as weakest."""


class EvaluationError(Exception):
    """The API answered, but not with what the rubric asked for."""


def points(composite: float) -> int:
    """Composite for display, floored so it never reads as meeting a bar it missed (74.6 shows as 74)."""
    return math.floor(composite)


@dataclass(frozen=True)
class DimensionOutcome:
    id: str
    label: str
    group: str
    type: Literal["score", "choice", "noul"]
    weight: float
    value: float | str
    """Expected score, chosen label, or P(yes)."""
    max_level: int | None = None
    normalized: float | None = None
    """Score as a share of its scale, or P(yes); None for choices."""
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    level: str | None = None
    """Description of the most likely score level or of the chosen label."""
    flag: str | None = None
    """Flag reason, when this answer tripped the dimension's flag."""

    def display(self) -> str:
        if self.type == "score":
            return f"{self.value:.1f}/{self.max_level}"
        if self.type == "noul":
            return f"{'yes' if float(self.value) >= 0.5 else 'no'} ({float(self.value):.0%})"
        return str(self.value)


@dataclass
class Decision:
    status: Status
    composite: float | None
    verdict: str | None
    verdict_confidence: float | None
    reasons: list[str]
    dimensions: list[DimensionOutcome]
    kind_match: float | None = None

    @property
    def weakest(self) -> list[DimensionOutcome]:
        weak = [
            outcome
            for outcome in self.dimensions
            if outcome.type == "score" and outcome.weight > 0 and (outcome.normalized or 0) < WEAK_BELOW
        ]
        return sorted(weak, key=lambda outcome: outcome.normalized or 0)[:3]


def unrouted(confidence: float) -> Decision:
    """No rubric fits the document, so nothing was scored."""
    return Decision(
        status="review",
        composite=None,
        verdict=None,
        verdict_confidence=None,
        reasons=[
            f"No rubric fits this document (router chose 'none', confidence {confidence:.2f}); "
            "pass --preset or --rubric"
        ],
        dimensions=[],
    )


def decide(
    rubric: Rubric,
    answers: Mapping[str, Answer],
    *,
    guards: Sequence[str] = (),
    truncated: bool = False,
) -> Decision:
    missing = [dim_id for dim_id in rubric.dimensions if dim_id not in answers]
    if missing:
        raise EvaluationError(f"response is missing answers for: {', '.join(missing)}")
    outcomes = [_outcome(dim_id, dim, answers[dim_id]) for dim_id, dim in rubric.dimensions.items()]
    composite = _composite(outcomes)
    verdict = next((outcome for outcome in outcomes if outcome.id == rubric.verdict_id), None)

    reasons: list[str] = []
    status = _base_status(rubric.policy, composite, verdict, reasons)
    for outcome in outcomes:
        flag = rubric.dimensions[outcome.id].flag
        if outcome.flag and flag is not None:
            status = _escalate(status, flag.action)
            reasons.append(outcome.flag)
    if truncated:
        status = _escalate(status, "review")
        reasons.append("Document was truncated for length; completeness was judged on partial text")

    # Guards question the evaluation itself, so they override the status rather than escalate it.
    invalid = list(guards)
    kind = answers.get(KIND_CHECK_ID)
    kind_match = kind.noul if isinstance(kind, NoulAnswer) else None
    if kind_match is not None and kind_match < 0.5:
        invalid.append(f"May not be a document this rubric covers (P={kind_match:.2f}); check the preset")
    if invalid:
        status, reasons = "review", invalid + reasons

    return Decision(
        status=status,
        composite=composite,
        verdict=str(verdict.value) if verdict else None,
        verdict_confidence=verdict.confidence if verdict else None,
        reasons=reasons,
        dimensions=outcomes,
        kind_match=kind_match,
    )


def _base_status(
    policy: Policy, composite: float | None, verdict: DimensionOutcome | None, reasons: list[str]
) -> Status:
    pass_at, fail_below = policy.pass_at, policy.fail_below
    if verdict is None:
        assert composite is not None  # rubric validation guarantees a verdict or a weighted dimension
        if composite >= pass_at:
            reasons.append(f"Composite {points(composite)} >= pass bar {pass_at:g}")
            return "pass"
        if composite < fail_below:
            reasons.append(f"Composite {points(composite)} < fail bar {fail_below:g}")
            return "fail"
        reasons.append(f"Composite {points(composite)} is between the fail bar {fail_below:g} and pass bar {pass_at:g}")
        return "revise"

    label, confidence = str(verdict.value), verdict.confidence or 0.0
    said = f"Verdict {label} ({confidence:.2f})"
    shown = "" if composite is None else str(points(composite))
    if composite is not None and (
        (label == "approve" and composite < fail_below) or (label == "reject" and composite >= pass_at)
    ):
        # Two independent signals disagree: escalate instead of picking one.
        reasons.append(f"{said} conflicts with composite {shown}")
        return "review"
    if composite is not None and composite < fail_below:
        # The composite fails on its own, whatever the verdict or its confidence.
        reasons.append(f"Composite {shown} < fail bar {fail_below:g}; {said.lower()}")
        return "fail"
    if confidence < policy.min_confidence:
        # The verdict decides the status from here, so an uncertain verdict goes to a person.
        reasons.append(f"Low-confidence verdict: {label} ({confidence:.2f} < {policy.min_confidence:g})")
        return "review"

    status: Status
    if composite is None:
        status = VERDICT_STATUS[label]
        reasons.append(said)
    elif label == "reject":
        status = "fail"
        reasons.append(f"{said}; composite {shown}")
    elif label == "approve" and composite >= pass_at:
        status = "pass"
        reasons.append(f"{said}; composite {shown} >= pass bar {pass_at:g}")
    elif label == "approve":
        status = "revise"
        reasons.append(f"{said}, but composite {shown} < pass bar {pass_at:g}")
    else:
        status = "revise"
        reasons.append(f"{said}; composite {shown}")
    return status


def _escalate(status: Status, to: Status) -> Status:
    return to if SEVERITY[to] > SEVERITY[status] else status


def _composite(outcomes: list[DimensionOutcome]) -> float | None:
    weighted = [(o.weight, o.normalized) for o in outcomes if o.weight > 0 and o.normalized is not None]
    total = sum(weight for weight, _ in weighted)
    if total == 0:
        return None
    # Rounded so float noise cannot drop an exact 75.0 to 74.99999999999999 below a bar.
    return round(100 * sum(weight * normalized for weight, normalized in weighted) / total, 6)


def _outcome(dim_id: str, dim: ScoreDimension | ChoiceDimension | NoulDimension, answer: Answer) -> DimensionOutcome:
    if answer.type != dim.type:
        raise EvaluationError(f"{dim_id}: expected a {dim.type} answer, got {answer.type}")
    common: dict[str, Any] = {"id": dim_id, "label": dim.label, "group": dim.group, "type": dim.type}

    if isinstance(dim, ScoreDimension) and isinstance(answer, ScoreAnswer):
        likely = max(answer.probabilities, key=answer.probabilities.__getitem__, default=round(answer.score))
        legend = answer.legend.get(likely, dim.criteria[likely] if 0 <= likely <= dim.max_level else None)
        below = dim.flag.below if dim.flag else None
        return DimensionOutcome(
            **common,
            weight=dim.weight,
            value=answer.score,
            max_level=dim.max_level,
            normalized=min(max(answer.score / dim.max_level, 0.0), 1.0),
            confidence=answer.confidence,
            probabilities={str(level): p for level, p in sorted(answer.probabilities.items())},
            level=_text(legend),
            flag=dim.flag.reason if dim.flag and below is not None and answer.score < below else None,
        )

    if isinstance(dim, ChoiceDimension) and isinstance(answer, ChoiceAnswer):
        if answer.choice not in dim.criteria:
            raise EvaluationError(f"{dim_id}: answer {answer.choice!r} is not one of the rubric's labels")
        return DimensionOutcome(
            **common,
            weight=0.0,
            value=answer.choice,
            confidence=answer.confidence,
            probabilities=dict(answer.probabilities),
            level=_text(dim.criteria.get(answer.choice)),
            flag=dim.flag.reason if dim.flag and answer.choice in (dim.flag.labels or ()) else None,
        )

    if isinstance(dim, NoulDimension) and isinstance(answer, NoulAnswer):
        p_yes = answer.noul
        flagged = False
        if dim.flag is not None:
            flagged = p_yes >= dim.flag.threshold if dim.flag.when == "yes" else p_yes < dim.flag.threshold
        outcomes = dim.criteria
        described = (outcomes.true if p_yes >= 0.5 else outcomes.false) if outcomes else None
        return DimensionOutcome(
            **common,
            weight=dim.weight,
            value=p_yes,
            normalized=p_yes,
            probabilities={"yes": p_yes, "no": 1 - p_yes},
            level=_text(described),
            flag=dim.flag.reason if dim.flag and flagged else None,
        )

    raise EvaluationError(f"{dim_id}: unexpected answer {answer!r}")


def _text(content: object) -> str | None:
    if content is None or isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)
