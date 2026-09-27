"""Rubrics: named dimensions that compile into TypeSafe System One questions.

A rubric is a YAML file. Each dimension is one narrow judgment asked as a TypeSafe Score, Choice,
or Noul question. All of a document's questions go out in one batched request; code (policy.py),
not the model, combines the answers into a composite score and a status.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Mapping
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator
from typesafe_sdk import Choice, Noul, NoulCriteria, Score

Content = str | dict[str, Any] | list[Any]
"""Instruction or criterion text: a string, or structured JSON when structure clarifies meaning."""

VERDICT_LABELS = ("approve", "revise", "reject")
KIND_CHECK_ID = "_kind_check"
ROUTE_ID = "_route"
NO_MATCH = "none"

_ID_PATTERN = r"[a-z][a-z0-9_]*"
_NAME_PATTERN = r"^[a-z][a-z0-9-]*$"


class RubricError(Exception):
    """A rubric is missing, unreadable, or invalid."""


class Flag(BaseModel):
    """Escalate a document's status when a dimension's answer lands on a flagged outcome."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    action: Literal["review", "fail"] = "review"
    labels: list[str] | None = None
    """Choice: flag when the chosen label is one of these."""
    when: Literal["yes", "no"] | None = None
    """Noul: flag when the answer is yes (P(yes) >= threshold) or no (P(yes) < threshold)."""
    threshold: float = Field(default=0.5, gt=0, lt=1)
    below: float | None = None
    """Score: flag when the expected score is below this level."""

    @field_validator("when", mode="before")
    @classmethod
    def _yes_no(cls, value: object) -> object:
        # YAML 1.1 reads bare `yes`/`no` as booleans.
        return ("yes" if value else "no") if isinstance(value, bool) else value


class _Dimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    group: str = "quality"
    instructions: Content
    flag: Flag | None = None


class ScoreDimension(_Dimension):
    type: Literal["score"]
    criteria: list[Content] = Field(min_length=2)
    """Ordered level descriptions; position is the score, starting at 0."""
    weight: float = Field(default=1.0, ge=0)

    @property
    def max_level(self) -> int:
        return len(self.criteria) - 1

    def question(self) -> Score:
        return Score(instructions=self.instructions, criteria=self.criteria)


class ChoiceDimension(_Dimension):
    type: Literal["choice"]
    criteria: dict[str, Content | None] = Field(min_length=2)
    role: Literal["verdict"] | None = None

    @field_validator("criteria", mode="before")
    @classmethod
    def _string_labels(cls, value: object) -> object:
        if isinstance(value, Mapping):
            for label in value:
                if not isinstance(label, str):
                    raise ValueError(f"choice label {label!r} is not a string; quote it in YAML (e.g. 'no': ...)")
        return value

    def question(self) -> Choice:
        return Choice(instructions=self.instructions, criteria=self.criteria)


class NoulOutcomes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    true: Content | None = None
    false: Content | None = None


class NoulDimension(_Dimension):
    type: Literal["noul"]
    criteria: NoulOutcomes | None = None
    weight: float = Field(default=0.0, ge=0)
    """When above zero, P(yes) contributes to the composite score."""

    @field_validator("criteria", mode="before")
    @classmethod
    def _bool_keys(cls, value: object) -> object:
        # YAML reads bare `true:`/`false:` keys as booleans.
        if isinstance(value, Mapping):
            return {str(key).lower() if isinstance(key, bool) else key: text for key, text in value.items()}
        return value

    def question(self) -> Noul:
        if self.criteria is None:
            return Noul(instructions=self.instructions)
        outcomes: NoulCriteria = {}
        if self.criteria.true is not None:
            outcomes["true"] = self.criteria.true
        if self.criteria.false is not None:
            outcomes["false"] = self.criteria.false
        return Noul(instructions=self.instructions, criteria=outcomes)


Dimension = Annotated[ScoreDimension | ChoiceDimension | NoulDimension, Field(discriminator="type")]


class Policy(BaseModel):
    """Code-owned thresholds that turn answers into a status."""

    model_config = ConfigDict(extra="forbid")

    pass_at: float = Field(default=75, ge=0, le=100)
    """Composite needed (with an approve verdict, when the rubric has one) to pass."""
    fail_below: float = Field(default=50, ge=0, le=100)
    """Composite below this fails."""
    min_confidence: float = Field(default=0.5, ge=0, le=1)
    """Verdict confidence below this sends the document to human review."""

    @model_validator(mode="after")
    def _ordered(self) -> Policy:
        if self.fail_below > self.pass_at:
            raise ValueError("fail_below must not exceed pass_at")
        return self


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME_PATTERN)
    title: str
    description: str
    document_kind: str
    """What documents this rubric is for. Sent as state; also used for routing and the kind check."""
    audience: str
    """Default intended audience, sent as state; `--audience` overrides it."""
    dimensions: dict[str, Dimension] = Field(min_length=1)
    policy: Policy = Field(default_factory=Policy)

    _source: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _check(self) -> Rubric:
        if self.name == NO_MATCH:
            raise ValueError(f"rubric name {NO_MATCH!r} is reserved")
        for dim_id, dim in self.dimensions.items():
            if not re.fullmatch(_ID_PATTERN, dim_id):
                raise ValueError(f"dimension id {dim_id!r} must match {_ID_PATTERN} (lowercase, digits, underscores)")
            _check_flag(dim_id, dim)
            try:  # the SDK's own question types are the contract; reject what the API would reject
                dim.question()
            except ValueError as error:
                detail = error.errors()[0]["msg"] if isinstance(error, ValidationError) else str(error)
                raise ValueError(f"{dim_id}: not a valid TypeSafe question ({detail})") from None
        verdicts = {
            dim_id: dim for dim_id, dim in self.dimensions.items() if isinstance(dim, ChoiceDimension) and dim.role
        }
        if len(verdicts) > 1:
            raise ValueError(f"only one dimension may have role: verdict (found {', '.join(verdicts)})")
        for dim_id, dim in verdicts.items():
            if set(dim.criteria) != set(VERDICT_LABELS):
                labels = ", ".join(VERDICT_LABELS)
                raise ValueError(f"verdict dimension {dim_id!r} must have exactly the labels {labels}")
        weighted = any(getattr(dim, "weight", 0) > 0 for dim in self.dimensions.values())
        if not verdicts and not weighted:
            raise ValueError("a rubric needs a verdict dimension or at least one weighted score/noul dimension")
        return self

    @property
    def source(self) -> Path | None:
        return self._source

    @property
    def verdict_id(self) -> str | None:
        return next(
            (dim_id for dim_id, dim in self.dimensions.items() if isinstance(dim, ChoiceDimension) and dim.role),
            None,
        )

    def questions(self, *, kind_check: bool) -> dict[str, Score | Choice | Noul]:
        """All of this rubric's questions for one batched request."""
        questions: dict[str, Score | Choice | Noul] = {
            dim_id: dim.question() for dim_id, dim in self.dimensions.items()
        }
        if kind_check:
            questions[KIND_CHECK_ID] = kind_check_question()
        return questions

    def with_policy(self, **overrides: float) -> Rubric:
        policy = Policy.model_validate(self.policy.model_dump() | overrides)
        rubric = self.model_copy(update={"policy": policy})
        rubric._source = self._source
        return rubric


def _check_flag(dim_id: str, dim: ScoreDimension | ChoiceDimension | NoulDimension) -> None:
    flag = dim.flag
    if flag is None:
        return
    if isinstance(dim, ChoiceDimension):
        unknown = set(flag.labels or ()) - set(dim.criteria)
        if not flag.labels or unknown:
            raise ValueError(f"{dim_id}: a choice flag needs `labels` from its criteria (unknown: {sorted(unknown)})")
    elif isinstance(dim, NoulDimension) and flag.when is None:
        raise ValueError(f"{dim_id}: a noul flag needs `when: yes` or `when: no`")
    elif isinstance(dim, ScoreDimension) and (flag.below is None or not 0 < flag.below <= dim.max_level):
        raise ValueError(f"{dim_id}: a score flag needs `below` between 0 and {dim.max_level}")


def kind_check_question() -> Noul:
    """Guard question: is the document the kind this rubric was written for?"""
    return Noul(
        instructions=(
            "`document.text` is the kind of document described in `evaluation_context.expected_document_kind`."
        ),
        criteria={
            "true": "It is that kind of document, even if it is incomplete, rough, or poorly written.",
            "false": "It is a different kind of document, or not a document of that kind at all.",
        },
    )


def routing_question(rubrics: list[Rubric]) -> Choice:
    """Choose which rubric fits a document, with an explicit no-match outcome."""
    criteria: dict[str, Content | None] = {rubric.name: rubric.document_kind for rubric in rubrics}
    criteria[NO_MATCH] = "None of the other descriptions fits; the text is some other kind of document."
    return Choice(
        instructions="Which description best fits the kind of document in `document_excerpt`?",
        criteria=criteria,
    )


# --- Loading ---------------------------------------------------------------------------------------------------------


def preset_names() -> list[str]:
    presets = resources.files("typesafe_doc_eval") / "presets"
    return sorted(entry.name.removesuffix(".yaml") for entry in presets.iterdir() if entry.name.endswith(".yaml"))


def preset_text(name: str) -> str:
    if name not in preset_names():
        raise RubricError(f"unknown preset {name!r}; available: {', '.join(preset_names())}")
    return (resources.files("typesafe_doc_eval") / "presets" / f"{name}.yaml").read_text(encoding="utf-8")


def load_preset(name: str) -> Rubric:
    rubric = parse_rubric(preset_text(name), origin=f"preset {name!r}")
    if rubric.name != name:
        raise RubricError(f"preset file {name}.yaml declares name {rubric.name!r}")
    return rubric


def load_rubric_file(path: Path) -> Rubric:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RubricError(f"cannot read rubric {path}: {error.strerror or error}") from error
    except UnicodeDecodeError as error:
        raise RubricError(f"cannot read rubric {path}: not UTF-8 ({error.reason} at byte {error.start})") from error
    rubric = parse_rubric(text, origin=str(path))
    rubric._source = path
    return rubric


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys, which YAML would otherwise resolve silently (last one wins)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, object]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if isinstance(key, Hashable) and key in seen:
                raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_rubric(text: str, *, origin: str) -> Rubric:
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise RubricError(f"{origin}: invalid YAML: {error}") from error
    if not isinstance(data, dict):
        raise RubricError(f"{origin}: expected a YAML mapping at the top level")
    try:
        return Rubric.model_validate(data)
    except ValidationError as error:
        problems = "\n".join(
            f"  - {'.'.join(str(part) for part in issue['loc']) or '(root)'}: {issue['msg']}"
            for issue in error.errors(include_url=False)
        )
        raise RubricError(f"{origin}: invalid rubric\n{problems}") from error


def resolve_rubrics(presets: list[str], files: list[Path]) -> list[Rubric]:
    """Rubrics for this run: named presets plus files; every preset when neither is given (or for `auto`)."""
    names = [name for entry in presets for name in entry.replace(",", " ").split()]
    if "auto" in names or (not names and not files):
        names = preset_names()
    rubrics = [load_preset(name) for name in dict.fromkeys(name for name in names if name != "auto")]
    rubrics += [load_rubric_file(path) for path in files]
    seen: set[str] = set()
    for rubric in rubrics:
        if rubric.name in seen:
            raise RubricError(f"two rubrics are named {rubric.name!r}; rubric names must be unique within a run")
        seen.add(rubric.name)
    return rubrics
