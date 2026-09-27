from pathlib import Path

import pytest
import yaml
from typesafe_sdk import Choice, Noul, Score

from typesafe_doc_eval.rubric import (
    KIND_CHECK_ID,
    NO_MATCH,
    ChoiceDimension,
    NoulDimension,
    RubricError,
    ScoreDimension,
    load_preset,
    parse_rubric,
    preset_names,
    resolve_rubrics,
    routing_question,
)

MINIMAL = """
name: minimal
title: Minimal
description: Test rubric.
document_kind: A test document.
audience: Testers.
dimensions:
  clarity:
    label: Clarity
    type: score
    instructions: How clear is `document.text`?
    criteria: [Unclear., Somewhat clear., Clear.]
"""


def rubric_with(extra: str, base: str = MINIMAL):
    return parse_rubric(base + extra, origin="test")


def test_presets_cover_the_four_document_families():
    assert preset_names() == ["deal-memo", "outbound", "product-spec", "property"]


@pytest.mark.parametrize("name", preset_names())
def test_presets_are_well_formed(name):
    rubric = load_preset(name)
    groups = {dim.group for dim in rubric.dimensions.values()}
    assert {"quality", "audience", "readiness", "verdict"} <= groups
    assert rubric.verdict_id == "verdict"
    for dim_id, dim in rubric.dimensions.items():
        if isinstance(dim, ScoreDimension):
            assert len(dim.criteria) == 5, dim_id
            assert all(isinstance(level, str) and level.endswith(".") for level in dim.criteria), dim_id
        # Question IDs never reach the model, so every instruction must reference the state it judges.
        assert "`document.text`" in str(dim.instructions) or "`document_excerpt`" in str(dim.instructions), dim_id
    questions = rubric.questions(kind_check=False)
    assert set(questions) == set(rubric.dimensions)
    assert all(isinstance(q, (Score, Choice, Noul)) for q in questions.values())


def test_kind_check_question_is_added_on_request():
    rubric = load_preset("property")
    assert KIND_CHECK_ID not in rubric.questions(kind_check=False)
    assert isinstance(rubric.questions(kind_check=True)[KIND_CHECK_ID], Noul)


def test_routing_question_offers_every_rubric_and_no_match():
    rubrics = [load_preset(name) for name in preset_names()]
    question = routing_question(rubrics)
    assert list(question.criteria) == [*preset_names(), NO_MATCH]


def test_show_rubric_round_trips(tmp_path: Path):
    from typesafe_doc_eval.rubric import load_rubric_file, preset_text

    path = tmp_path / "mine.yaml"
    path.write_text(preset_text("outbound"), encoding="utf-8")
    assert load_rubric_file(path).model_dump() == load_preset("outbound").model_dump()


def test_yaml_booleans_are_coerced_for_noul_criteria_and_flags():
    rubric = rubric_with(
        """
  has_summary:
    label: Summary
    type: noul
    weight: 0.5
    instructions: "`document.text` opens with a summary."
    criteria:
      true: It opens with a summary.
      false: It does not.
    flag:
      when: no
      reason: No summary.
"""
    )
    dim = rubric.dimensions["has_summary"]
    assert isinstance(dim, NoulDimension)
    assert dim.flag is not None and dim.flag.when == "no"
    assert dim.question().model_dump()["criteria"] == {"true": "It opens with a summary.", "false": "It does not."}


def test_choice_label_parsed_as_boolean_explains_how_to_fix():
    with pytest.raises(RubricError, match="quote it"):
        rubric_with(
            """
  answered:
    label: Answered
    type: choice
    instructions: Does `document.text` answer the question?
    criteria:
      yes: It does.
      no: It does not.
"""
        )


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            """
  verdict:
    label: Verdict
    type: choice
    role: verdict
    instructions: Ship `document.text`?
    criteria: {ship: Yes., hold: No.}
""",
            "exactly the labels approve, revise, reject",
        ),
        (
            """
  tone:
    label: Tone
    type: choice
    instructions: Tone of `document.text`?
    criteria: {calm: Calm., angry: Angry.}
    flag: {labels: [furious], reason: Too angry.}
""",
            "labels",
        ),
        (
            """
  Bad-Id:
    label: Bad
    type: score
    instructions: x
    criteria: [a, b]
""",
            "must match",
        ),
        ("\npolicy: {pass_at: 40, fail_below: 60}\n", "fail_below must not exceed pass_at"),
        ("\nunexpected: true\n", "Extra inputs are not permitted"),
    ],
)
def test_invalid_rubrics_are_rejected_with_a_located_message(extra, message):
    with pytest.raises(RubricError, match=message):
        rubric_with(extra)


def test_rubric_needs_a_verdict_or_a_weighted_dimension():
    unweighted = MINIMAL.replace("    type: score\n", "    type: score\n    weight: 0\n")
    with pytest.raises(RubricError, match="verdict dimension or at least one weighted"):
        parse_rubric(unweighted, origin="test")


def test_invalid_yaml_is_reported():
    with pytest.raises(RubricError, match="invalid YAML"):
        parse_rubric("name: [unclosed", origin="broken.yaml")


def test_resolve_rubrics(tmp_path: Path):
    custom = tmp_path / "custom.yaml"
    custom.write_text(MINIMAL, encoding="utf-8")

    assert [r.name for r in resolve_rubrics([], [])] == preset_names()
    assert [r.name for r in resolve_rubrics(["property,outbound"], [])] == ["property", "outbound"]
    assert [r.name for r in resolve_rubrics([], [custom])] == ["minimal"]
    assert [r.name for r in resolve_rubrics(["auto"], [custom])] == [*preset_names(), "minimal"]
    with pytest.raises(RubricError, match="unknown preset"):
        resolve_rubrics(["nope"], [])
    with pytest.raises(RubricError, match="unique"):
        resolve_rubrics([], [custom, custom])


def test_with_policy_overrides_and_validates():
    rubric = load_preset("property")
    assert rubric.with_policy(pass_at=90).policy.pass_at == 90
    with pytest.raises(ValueError):
        rubric.with_policy(fail_below=95)


def test_choice_dimension_question_keeps_descriptions():
    dim = load_preset("property").dimensions["tone"]
    assert isinstance(dim, ChoiceDimension)
    assert yaml.safe_dump(dim.question().model_dump()["criteria"])


@pytest.mark.parametrize("path", sorted((Path(__file__).parent.parent / "examples" / "rubrics").glob("*.yaml")))
def test_example_rubrics_are_valid(path: Path):
    from typesafe_doc_eval.rubric import load_rubric_file

    rubric = load_rubric_file(path)
    assert rubric.verdict_id and {dim.type for dim in rubric.dimensions.values()} == {"score", "noul", "choice"}
