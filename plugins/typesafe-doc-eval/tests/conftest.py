"""A scripted stand-in for the TypeSafe API, served through the real SDK client."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from typesafe_sdk import RetryPolicy

from typesafe_doc_eval import cli

Answerer = Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]
"""(question name, question JSON, request body) -> answer JSON."""


def score_json(value: float, levels: int = 5, confidence: float = 0.8) -> dict[str, Any]:
    likely = round(value)
    rest = (1 - confidence) / (levels - 1)
    return {
        "type": "score",
        "score": value,
        "confidence": confidence,
        "legend": {str(level): f"level {level}" for level in range(levels)},
        "probabilities": {str(level): confidence if level == likely else rest for level in range(levels)},
    }


def choice_json(pick: str, labels: list[str], confidence: float = 0.9) -> dict[str, Any]:
    rest = (1 - confidence) / (len(labels) - 1)
    return {
        "type": "choice",
        "choice": pick,
        "confidence": confidence,
        "probabilities": {label: confidence if label == pick else rest for label in labels},
    }


def strong_document(name: str, question: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Answers for a document that should pass: high scores, approve, first (positive) labels, yes."""
    if question["type"] == "score":
        levels = len(question["criteria"])
        return score_json(levels - 1.2, levels)
    if question["type"] == "choice":
        labels = list(question["criteria"])
        return choice_json("approve" if "approve" in labels else labels[0], labels)
    return {"type": "noul", "noul": 0.95}


class ScriptedAPI:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.answer: Answerer = strong_document
        self.status = 200
        self.raise_error: Exception | None = None

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.raise_error is not None:
            raise self.raise_error
        if self.status != 200:
            return httpx2.Response(self.status, json={"detail": "scripted failure"})
        answers = {name: self.answer(name, question, body) for name, question in body["questions"].items()}
        usage = {"input_tokens": 100, "output_tokens": len(answers)}
        return httpx2.Response(200, json={"model": "jev-test", "answers": answers, "usage": usage})


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> ScriptedAPI:
    """Route the CLI's client through a scripted API (no network, no key needed)."""
    scripted = ScriptedAPI()
    real_client = cli.AsyncTypeSafeClient

    def client(**kwargs: Any) -> Any:
        kwargs["transport"] = kwargs.get("transport") or scripted.transport()
        kwargs["api_key"] = kwargs.get("api_key") or "test-key"
        kwargs["retry"] = RetryPolicy(max_retries=0)
        return real_client(**kwargs)

    monkeypatch.setattr(cli, "AsyncTypeSafeClient", client)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return scripted


@pytest.fixture
def lease(tmp_path):
    path = tmp_path / "lease.md"
    path.write_text(
        "# Lease\n\nTenant Jordan Ellis (jordan@example.com, 212-555-0100) rents Unit 4B for $2,850 per month, "
        "due on the 1st, from October 1, 2026 to September 30, 2027.\n",
        encoding="utf-8",
    )
    return path
