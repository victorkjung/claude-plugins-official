"""Synthetic answers for `--mock`.

A drop-in HTTP transport for the real SDK client, so a mock run exercises the same request and
response path as a live one. Answers are random but deterministic per document. They are NOT
evaluations: use them to preview output and wire up pipelines without an API key.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from typing import Any

import httpx2

from typesafe_doc_eval.rubric import NO_MATCH, ROUTE_ID, VERDICT_LABELS

MOCK_MODEL = "mock-synthetic"

_ROUTE_HINTS = {
    "property": ("lease", "tenant", "landlord", "premises", "security deposit", "inspection", "work order", "vendor"),
    "deal-memo": ("investment", "irr", "valuation", "underwriting", "cap rate", "noi", "thesis", "term sheet"),
    "product-spec": ("requirements", "prd", "rfc", "design doc", "runbook", "postmortem", "incident", "milestone"),
    "outbound": ("subscribe", "sign up", "book a", "just listed", "newsletter", "act now", "discover", "don't miss"),
}


def transport() -> httpx2.MockTransport:
    return httpx2.MockTransport(_respond)


def _rng(*parts: str) -> random.Random:
    return random.Random(hashlib.sha256("\x1f".join(parts).encode()).hexdigest())


def _respond(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)
    state = json.dumps(body["state"], sort_keys=True)
    quality = _rng(state).uniform(0.35, 0.95)  # a latent quality per document keeps answers coherent
    answers: dict[str, Any] = {}
    shares: list[float] = []

    for name, question in body["questions"].items():
        rng = _rng(state, name)
        if question["type"] == "score":
            levels = len(question["criteria"])
            target = min(max(quality + rng.uniform(-0.2, 0.2), 0.0), 1.0) * (levels - 1)
            weights = [math.exp(-((level - target) ** 2) / 0.5) for level in range(levels)]
            probabilities = [weight / sum(weights) for weight in weights]
            score = sum(level * p for level, p in enumerate(probabilities))
            shares.append(score / (levels - 1))
            answers[name] = {
                "type": "score",
                "score": score,
                "confidence": max(probabilities),
                "legend": {str(level): text for level, text in enumerate(question["criteria"])},
                "probabilities": {str(level): p for level, p in enumerate(probabilities)},
            }
        elif question["type"] == "noul":
            answers[name] = {"type": "noul", "noul": round(rng.uniform(0.85, 0.99), 3)}

    for name, question in body["questions"].items():
        if question["type"] != "choice":
            continue
        rng = _rng(state, name)
        labels = list(question["criteria"])
        if set(labels) == set(VERDICT_LABELS):
            mean = sum(shares) / len(shares) if shares else quality
            pick = "approve" if mean >= 0.75 else "revise" if mean >= 0.5 else "reject"
        elif name == ROUTE_ID:
            pick = _route(labels, state.lower())
        else:
            pick = rng.choices(labels, weights=[max(4 - rank, 0.5) for rank in range(len(labels))])[0]
        confidence = round(rng.uniform(0.55, 0.95), 3)
        rest = (1 - confidence) / (len(labels) - 1)
        answers[name] = {
            "type": "choice",
            "choice": pick,
            "confidence": confidence,
            "probabilities": {label: confidence if label == pick else rest for label in labels},
        }

    usage = {"input_tokens": len(request.content) // 4, "output_tokens": len(answers)}
    return httpx2.Response(200, json={"model": MOCK_MODEL, "answers": answers, "usage": usage})


def _route(labels: list[str], text: str) -> str:
    hits = {label: sum(hint in text for hint in _ROUTE_HINTS.get(label, ())) for label in labels}
    best = max(hits, key=hits.__getitem__)
    return best if hits[best] else NO_MATCH if NO_MATCH in labels else labels[0]
