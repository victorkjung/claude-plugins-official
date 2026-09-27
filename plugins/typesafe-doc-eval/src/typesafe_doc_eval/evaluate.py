"""Batched System One requests.

Each document gets one request carrying every question in its rubric; the questions run in
parallel server-side and cannot see each other's answers. When several rubrics are in play, a
small routing request (a Choice over the rubrics' `document_kind`, on the document's opening)
picks the rubric first, because its answer decides which questions to ask next.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    ChoiceAnswer,
    Noul,
    Score,
    SystemOneResponse,
    TypeSafeAPIConnectionError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

from typesafe_doc_eval.extract import PreparedDocument
from typesafe_doc_eval.policy import Decision, EvaluationError, decide, unrouted
from typesafe_doc_eval.rubric import NO_MATCH, ROUTE_ID, Rubric, routing_question

ROUTE_EXCERPT_CHARS = 6_000
"""Routing only needs the opening of a document to recognize its kind."""
ROUTE_MIN_CONFIDENCE = 0.5
"""Routing below this confidence still evaluates, but sends the document to review."""


@dataclass(frozen=True)
class Options:
    model: str | None = None
    audience: str | None = None
    context: str | None = None
    kind_check: bool = True
    concurrency: int = 4


@dataclass
class DocumentResult:
    document: PreparedDocument
    rubric: Rubric | None = None
    route: ChoiceAnswer | None = None
    decision: Decision | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def status(self) -> str:
        return self.decision.status if self.decision and not self.error else "error"

    def account(self, response: SystemOneResponse) -> None:
        self.model = response.model
        self.input_tokens += response.usage.input_tokens or 0
        self.output_tokens += response.usage.output_tokens or 0


@dataclass(frozen=True)
class PlannedRequest:
    purpose: str
    state: Any
    questions: dict[str, Score | Choice | Noul]
    conditional: bool = False
    """Sent only if routing picks this request's rubric."""


def document_state(doc: PreparedDocument, rubric: Rubric, options: Options) -> dict[str, Any]:
    """Named fields that questions reference by path, such as `document.text`."""
    document: dict[str, Any] = {"text": doc.text}
    notes = []
    if doc.truncated:
        notes.append("Part of the middle of this document was omitted for length where marked; judge the text shown.")
    if doc.redactions:
        notes.append(
            "Personal data and secrets were replaced with placeholders such as [EMAIL] or [PHONE]; "
            "treat a placeholder as the value being present."
        )
    if notes:
        document["notes"] = notes
    context: dict[str, Any] = {
        "expected_document_kind": rubric.document_kind,
        "intended_audience": options.audience or rubric.audience,
    }
    if options.context:
        context["background"] = options.context
    return {"document": document, "evaluation_context": context}


def routing_state(doc: PreparedDocument) -> dict[str, Any]:
    state: dict[str, Any] = {"document_excerpt": doc.text[:ROUTE_EXCERPT_CHARS]}
    if len(doc.text) > ROUTE_EXCERPT_CHARS:
        state["note"] = f"The first {ROUTE_EXCERPT_CHARS:,} characters of a longer document."
    return state


def plan(doc: PreparedDocument, rubrics: list[Rubric], options: Options) -> list[PlannedRequest]:
    """The requests a document needs, for --dry-run. With routing, one of the conditional requests follows."""
    routed = len(rubrics) > 1
    evaluations = [
        PlannedRequest(
            f"evaluate:{rubric.name}",
            document_state(doc, rubric, options),
            rubric.questions(kind_check=options.kind_check and not routed),
            conditional=routed,
        )
        for rubric in rubrics
    ]
    if not routed:
        return evaluations
    return [PlannedRequest("route", routing_state(doc), {ROUTE_ID: routing_question(rubrics)}), *evaluations]


async def evaluate_all(
    client: AsyncTypeSafeClient,
    docs: list[PreparedDocument],
    rubrics: list[Rubric],
    options: Options,
    on_done: Callable[[DocumentResult], None] | None = None,
) -> list[DocumentResult]:
    semaphore = asyncio.Semaphore(max(1, options.concurrency))

    async def run(doc: PreparedDocument) -> DocumentResult:
        async with semaphore:
            result = await evaluate_document(client, doc, rubrics, options)
        if on_done is not None:
            on_done(result)
        return result

    return list(await asyncio.gather(*(run(doc) for doc in docs)))


async def evaluate_document(
    client: AsyncTypeSafeClient, doc: PreparedDocument, rubrics: list[Rubric], options: Options
) -> DocumentResult:
    result = DocumentResult(document=doc)
    if doc.error:
        result.error = doc.error
        return result
    try:
        guards: list[str] = []
        if len(rubrics) == 1:
            rubric = rubrics[0]
        else:
            response = await client.system_one(
                routing_state(doc), {ROUTE_ID: routing_question(rubrics)}, model=options.model
            )
            result.account(response)
            route = response.choices.get(ROUTE_ID)
            if route is None:
                raise EvaluationError("routing response has no answer")
            result.route = route
            if route.choice == NO_MATCH:
                result.decision = unrouted(route.confidence)
                return result
            by_name = {rubric.name: rubric for rubric in rubrics}
            if route.choice not in by_name:
                raise EvaluationError(f"router chose unknown rubric {route.choice!r}")
            rubric = by_name[route.choice]
            if route.confidence < ROUTE_MIN_CONFIDENCE:
                guards.append(
                    f"Document type uncertain: routed to {rubric.name} with confidence {route.confidence:.2f}"
                )
        result.rubric = rubric
        response = await client.system_one(
            document_state(doc, rubric, options),
            rubric.questions(kind_check=options.kind_check and len(rubrics) == 1),
            model=options.model,
        )
        result.account(response)
        result.decision = decide(rubric, response.answers, guards=guards, truncated=doc.truncated)
    except TypeSafeError as error:
        result.error = describe_error(error)
    except EvaluationError as error:
        result.error = str(error)
    except Exception as error:  # one bad document must not sink a batch; it is reported and exits 3
        result.error = f"unexpected {type(error).__name__}: {error}"
    return result


def describe_error(error: TypeSafeError) -> str:
    if isinstance(error, TypeSafeAuthenticationError):
        return f"authentication failed; check TYPESAFE_API_KEY ({error})"
    if isinstance(error, TypeSafePermissionDeniedError):
        return f"permission denied for this API key or model ({error})"
    if isinstance(error, TypeSafeAPITimeoutError):
        return f"request timed out; raise --timeout for long documents ({error})"
    if isinstance(error, TypeSafeAPIConnectionError):
        return f"cannot reach the TypeSafe API; check network access to api.typesafe.ai ({error})"
    if isinstance(error, TypeSafeRateLimitError):
        return f"rate limited after retries; lower --concurrency ({error})"
    return str(error)
