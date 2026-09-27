import pytest
from typesafe_sdk import Answer, ChoiceAnswer, NoulAnswer, ScoreAnswer

from typesafe_doc_eval.policy import EvaluationError, decide
from typesafe_doc_eval.rubric import KIND_CHECK_ID, load_preset, parse_rubric

PROPERTY = load_preset("property")
SCORE_IDS = [dim_id for dim_id, dim in PROPERTY.dimensions.items() if dim.type == "score"]


def score(value: float, levels: int = 5, confidence: float = 0.8) -> ScoreAnswer:
    likely = round(value)
    rest = (1 - confidence) / (levels - 1)
    return ScoreAnswer(
        type="score",
        score=value,
        confidence=confidence,
        legend={level: f"level {level}" for level in range(levels)},
        probabilities={level: confidence if level == likely else rest for level in range(levels)},
    )


def choice(pick: str, labels: list[str], confidence: float = 0.9) -> ChoiceAnswer:
    rest = (1 - confidence) / (len(labels) - 1)
    return ChoiceAnswer(
        type="choice",
        choice=pick,
        confidence=confidence,
        probabilities={label: confidence if label == pick else rest for label in labels},
    )


def answers(composite: float, verdict: str = "approve", verdict_confidence: float = 0.9, tone: str = "professional"):
    """Property answers where every weighted score sits at `composite`% of its scale."""
    result: dict[str, Answer] = {dim_id: score(4 * composite / 100) for dim_id in SCORE_IDS}
    result["tone"] = choice(tone, ["professional", "friendly", "legalistic", "hostile"])
    result["verdict"] = choice(verdict, ["approve", "revise", "reject"], verdict_confidence)
    return result


@pytest.mark.parametrize(
    ("verdict", "confidence", "composite", "status"),
    [
        ("approve", 0.9, 85, "pass"),
        ("approve", 0.9, 65, "revise"),  # approve alone is not enough below the pass bar
        ("revise", 0.9, 90, "revise"),
        ("reject", 0.9, 40, "fail"),
        ("revise", 0.9, 45, "fail"),  # composite below the fail bar fails regardless
        ("reject", 0.9, 80, "review"),  # verdict and composite disagree: a person decides
        ("approve", 0.9, 30, "review"),
        ("approve", 0.4, 85, "review"),  # low-confidence verdict
        ("revise", 0.4, 45, "fail"),  # a clear fail stays a fail
    ],
)
def test_status_table(verdict, confidence, composite, status):
    decision = decide(PROPERTY, answers(composite, verdict, confidence))
    assert decision.status == status, decision.reasons
    assert decision.composite == pytest.approx(composite)
    assert decision.reasons


def test_flagged_choice_escalates_to_review():
    decision = decide(PROPERTY, answers(90, tone="hostile"))
    assert decision.status == "review"
    assert "Tone reads as hostile or threatening toward the reader." in decision.reasons
    assert next(o for o in decision.dimensions if o.id == "tone").flag


def test_kind_check_overrides_even_a_failing_status():
    base = answers(20, "reject")
    assert decide(PROPERTY, base).status == "fail"
    decision = decide(PROPERTY, {**base, KIND_CHECK_ID: NoulAnswer(type="noul", noul=0.1)})
    assert decision.status == "review"
    assert decision.reasons[0].startswith("May not be a document this rubric covers")
    assert decision.kind_match == pytest.approx(0.1)


def test_guards_and_truncation():
    assert decide(PROPERTY, answers(90), guards=["Document type uncertain"]).status == "review"
    truncated = decide(PROPERTY, answers(90), truncated=True)
    assert truncated.status == "review" and "truncated" in truncated.reasons[-1]
    assert decide(PROPERTY, answers(20, "reject"), truncated=True).status == "fail"


def test_composite_is_weighted_and_weakest_are_listed():
    result = answers(100)
    result["completeness"] = score(0.0)  # weight 1.5 of 5.75 total
    decision = decide(PROPERTY, result)
    assert decision.composite == pytest.approx(100 * (5.75 - 1.5) / 5.75)
    assert [o.id for o in decision.weakest] == ["completeness"]
    completeness = next(o for o in decision.dimensions if o.id == "completeness")
    assert completeness.level == "level 0" and completeness.display() == "0.0/4"


def test_rubric_without_verdict_uses_composite_thresholds_and_weighted_nouls():
    rubric = parse_rubric(
        """
name: bare
title: Bare
description: d
document_kind: k
audience: a
dimensions:
  clarity: {label: Clarity, type: score, instructions: "`document.text`?", criteria: [a, b, c]}
  has_ask:
    label: Has ask
    type: noul
    weight: 1
    instructions: "`document.text` makes an ask."
    flag: {when: "no", reason: No ask., action: fail}
""",
        origin="test",
    )
    passing = decide(rubric, {"clarity": score(2.0, 3), "has_ask": NoulAnswer(type="noul", noul=0.9)})
    assert passing.status == "pass" and passing.composite == pytest.approx(95)
    failing = decide(rubric, {"clarity": score(2.0, 3), "has_ask": NoulAnswer(type="noul", noul=0.2)})
    assert failing.status == "fail" and "No ask." in failing.reasons


def test_missing_or_mistyped_answers_are_errors():
    incomplete = answers(90)
    del incomplete["clarity"]
    with pytest.raises(EvaluationError, match="missing answers for: clarity"):
        decide(PROPERTY, incomplete)
    mistyped = answers(90)
    mistyped["clarity"] = NoulAnswer(type="noul", noul=0.5)
    with pytest.raises(EvaluationError, match="expected a score answer"):
        decide(PROPERTY, mistyped)
