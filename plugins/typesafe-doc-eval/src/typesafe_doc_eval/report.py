"""Render results as a terminal scorecard, JSON, or Markdown."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.measure import Measurement
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from typesafe_doc_eval import __version__
from typesafe_doc_eval.evaluate import DocumentResult, PlannedRequest
from typesafe_doc_eval.extract import PreparedDocument
from typesafe_doc_eval.policy import DimensionOutcome, points
from typesafe_doc_eval.rubric import Rubric, ScoreDimension

STATUSES = ("pass", "revise", "review", "fail", "error")
STATUS_STYLE = {
    "pass": "bold green",
    "revise": "bold yellow",
    "review": "bold magenta",
    "fail": "bold red",
    "error": "bold red",
}
STATUS_ICON = {"pass": "✅", "revise": "✏️", "review": "👀", "fail": "❌", "error": "⚠️"}
VERDICT_STYLE = {"approve": "green", "revise": "yellow", "reject": "red"}
MOCK_BANNER = "MOCK MODE: synthetic answers, not real evaluations"


@dataclass(frozen=True)
class RunInfo:
    rubrics: list[Rubric]
    mock: bool = False

    @property
    def routed(self) -> bool:
        return len(self.rubrics) > 1


def summarize(results: Sequence[DocumentResult]) -> dict[str, Any]:
    counts: dict[str, int] = dict.fromkeys(STATUSES, 0)
    for result in results:
        counts[result.status] += 1
    return {
        "documents": len(results),
        **counts,
        "input_tokens": sum(result.input_tokens for result in results),
        "output_tokens": sum(result.output_tokens for result in results),
    }


def _models(results: Sequence[DocumentResult]) -> str:
    return ", ".join(sorted({result.model for result in results if result.model})) or "n/a"


def _summary_line(results: Sequence[DocumentResult]) -> str:
    summary = summarize(results)
    parts = [f"{summary[status]} {status}" for status in STATUSES if summary[status]]
    return (
        f"{summary['documents']} document{'s' * (summary['documents'] != 1)}: {' · '.join(parts)}"
        f" · {summary['input_tokens']:,} input tokens · model {_models(results)}"
    )


def display_names(paths: Sequence[Path]) -> dict[Path, str]:
    """Paths relative to the inputs' common directory, so terminal tables don't repeat it on every row."""
    try:
        base = os.path.commonpath([os.path.dirname(os.path.abspath(path)) for path in paths])
    except ValueError:  # no paths, or paths on different drives
        return {path: str(path) for path in paths}
    return {path: os.path.relpath(os.path.abspath(path), base) for path in paths}


# --- JSON ------------------------------------------------------------------------------------------------------------


def to_json(results: Sequence[DocumentResult], info: RunInfo) -> str:
    payload = {
        "tool": "typesafe-doc-eval",
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mock": info.mock,
        "summary": summarize(results),
        "documents": [_document_json(result) for result in results],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _document_json(result: DocumentResult) -> dict[str, Any]:
    decision, doc, route = result.decision, result.document, result.route
    return {
        "path": str(doc.path),
        "status": result.status,
        "rubric": result.rubric.name if result.rubric else None,
        "composite": _round(decision.composite) if decision else None,
        "verdict": {"label": decision.verdict, "confidence": _round(decision.verdict_confidence)}
        if decision and decision.verdict
        else None,
        "reasons": decision.reasons if decision else [],
        "weakest": [outcome.id for outcome in decision.weakest] if decision else [],
        "routing": {
            "choice": route.choice,
            "confidence": _round(route.confidence),
            "probabilities": {k: _round(v) for k, v in route.probabilities.items()},
        }
        if route
        else None,
        "kind_match": _round(decision.kind_match) if decision else None,
        "dimensions": {outcome.id: _dimension_json(outcome) for outcome in decision.dimensions} if decision else {},
        "input": {
            "chars": doc.original_chars,
            "sent_chars": len(doc.text),
            "truncated": doc.truncated,
            "redactions": doc.redactions,
        },
        "model": result.model,
        "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
        "error": result.error,
    }


def _dimension_json(outcome: DimensionOutcome) -> dict[str, Any]:
    data: dict[str, Any] = {"label": outcome.label, "group": outcome.group, "type": outcome.type}
    if outcome.type == "score":
        data |= {
            "score": _round(outcome.value),
            "max": outcome.max_level,
            "normalized": _round(outcome.normalized),
            "weight": outcome.weight,
            "confidence": _round(outcome.confidence),
            "level": outcome.level,
        }
    elif outcome.type == "choice":
        data |= {"choice": outcome.value, "confidence": _round(outcome.confidence), "description": outcome.level}
    else:
        data |= {"p_yes": _round(outcome.value), "weight": outcome.weight, "description": outcome.level}
    data["probabilities"] = {key: _round(value) for key, value in outcome.probabilities.items()}
    if outcome.flag:
        data["flag"] = outcome.flag
    return data


def _round(value: object) -> Any:
    return round(value, 4) if isinstance(value, float) else value


# --- Markdown --------------------------------------------------------------------------------------------------------


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _verdict_text(result: DocumentResult) -> str:
    decision = result.decision
    if not decision or not decision.verdict:
        return "—"
    return f"{decision.verdict} ({decision.verdict_confidence or 0:.2f})"


def _composite_text(result: DocumentResult) -> str:
    decision = result.decision
    return str(points(decision.composite)) if decision and decision.composite is not None else "—"


def to_markdown(results: Sequence[DocumentResult], info: RunInfo) -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["# Document evaluation", ""]
    if info.mock:
        lines += [f"> **{MOCK_BANNER}.**", ""]
    lines += [f"_{generated} · typesafe-doc-eval {__version__}_", "", f"**{_summary_line(results)}**", ""]
    lines += ["| Document | Rubric | Status | Composite | Verdict | Weakest |", "|---|---|---|---:|---|---|"]
    for result in results:
        weakest = ", ".join(o.label for o in result.decision.weakest) if result.decision else ""
        lines.append(
            f"| {_cell(result.document.path)} | {result.rubric.name if result.rubric else '—'} "
            f"| {STATUS_ICON[result.status]} {result.status} | {_composite_text(result)} "
            f"| {_cell(_verdict_text(result))} | {_cell(weakest) or '—'} |"
        )
    for result in results:
        lines += ["", f"## {_cell(result.document.path)}: {result.status.upper()}", ""]
        if result.error:
            lines += [f"Not evaluated: {result.error}"]
            continue
        decision = result.decision
        assert decision is not None
        facts = []
        if result.rubric:
            facts.append(f"Rubric **{result.rubric.title}**")
        if result.route and result.rubric:
            facts.append(f"routed with confidence {result.route.confidence:.2f}")
        if decision.composite is not None:
            facts.append(f"composite **{points(decision.composite)}**/100")
        if decision.verdict:
            facts.append(f"verdict **{decision.verdict}** ({decision.verdict_confidence or 0:.2f})")
        lines += [" · ".join(facts), ""]
        lines += [f"- {_cell(reason)}" for reason in decision.reasons]
        if decision.dimensions:
            lines += ["", "| Dimension | Result | Confidence | Most likely level |", "|---|---|---:|---|"]
            for outcome in decision.dimensions:
                confidence = f"{outcome.confidence:.2f}" if outcome.confidence is not None else "—"
                flag = " ⚑" if outcome.flag else ""
                lines.append(
                    f"| {_cell(outcome.label)} | {_cell(outcome.display())}{flag} | {confidence} "
                    f"| {_cell(outcome.level or '')} |"
                )
        notes = _input_notes(result.document)
        if notes:
            lines += ["", f"_Input: {'; '.join(notes)}_"]
    return "\n".join(lines) + "\n"


def _input_notes(doc: PreparedDocument) -> list[str]:
    notes = []
    if doc.truncated:
        notes.append(f"truncated to {len(doc.text):,} of {doc.original_chars:,} characters")
    if doc.redactions:
        notes.append("redacted " + ", ".join(f"{count} {kind}" for kind, count in sorted(doc.redactions.items())))
    return notes


# --- Terminal --------------------------------------------------------------------------------------------------------


def _share_style(share: float | None) -> str:
    if share is None:
        return ""
    return "green" if share >= 0.75 else "yellow" if share >= 0.5 else "red"


def _outcome_cell(outcome: DimensionOutcome) -> Text:
    if outcome.flag:
        return Text(f"⚑ {outcome.display()}", style="bold red")
    if outcome.type == "choice":
        low = outcome.confidence is not None and outcome.confidence < 0.5
        return Text(str(outcome.value) + ("?" if low else ""), style="dim" if low else "")
    if outcome.type == "score":
        return Text(f"{outcome.value:.1f}", style=_share_style(outcome.normalized))
    return Text(outcome.display(), style=_share_style(outcome.normalized) if outcome.weight else "")


def render_table(console: Console, results: Sequence[DocumentResult], info: RunInfo, *, details: bool) -> None:
    names = display_names([result.document.path for result in results])
    if info.mock:
        console.print(Panel(Text(MOCK_BANNER, style="bold yellow"), expand=False))
    for rubric in info.rubrics:
        rows = [result for result in results if result.rubric is rubric and not result.error and result.decision]
        if rows:
            console.print(_rubric_view(console, rubric, rows, names))
    unscored = [result for result in results if result.error or not result.rubric]
    if unscored:
        table = Table(title="Not evaluated", title_justify="left", title_style="bold")
        table.add_column("Document", overflow="fold", min_width=_name_width(names))
        table.add_column("Status")
        table.add_column("Detail", overflow="fold")
        for result in unscored:
            detail = result.error or "; ".join(result.decision.reasons if result.decision else [])
            table.add_row(names[result.document.path], Text(result.status, style=STATUS_STYLE[result.status]), detail)
        console.print(table)

    attention = [r for r in results if r.status not in ("pass", "error") and r.decision and r.rubric]
    if attention:
        console.print(Text("Why", style="bold"))
        for result in attention:
            decision = result.decision
            assert decision is not None
            line = Text("  ")
            line.append(f"{result.status.upper():7}", style=STATUS_STYLE[result.status])
            line.append(f" {names[result.document.path]}: ")
            line.append("; ".join(decision.reasons))
            if decision.weakest:
                line.append(
                    "  weakest: " + ", ".join(f"{o.label} {o.value:.1f}/{o.max_level}" for o in decision.weakest),
                    style="dim",
                )
            console.print(line)

    if details:
        for result in results:
            if result.decision and result.rubric and not result.error:
                console.print(_details_panel(result, names[result.document.path]))
    console.print(Text(_summary_line(results), style="bold"))


def _name_width(names: dict[Path, str]) -> int:
    return min(max((len(name) for name in names.values()), default=8), 24)


def _rubric_view(console: Console, rubric: Rubric, rows: Sequence[DocumentResult], names: dict[Path, str]) -> Table:
    """Documents x dimensions when that fits the console; else a column per document; else a compact summary."""
    # Measure unclamped (Measurement.get clamps to the width it is given), and require the natural width:
    # rich squeezes and crops columns well before it reaches the minimum it reports.
    unclamped = console.options.update(width=10_000)
    for build in (_matrix, _transposed):
        table = build(rubric, rows, names)
        if Measurement.get(console, unclamped, table).maximum <= console.width:
            return table
    return _compact(rubric, rows, names)


def _table(rubric: Rubric, note: str = "") -> Table:
    policy = rubric.policy
    scales = sorted({dim.max_level for dim in rubric.dimensions.values() if isinstance(dim, ScoreDimension)})
    caption = (
        f"scores {'/'.join(f'0-{top}' for top in scales)} · composite 0-100: "
        f"pass >= {policy.pass_at:g}, fail < {policy.fail_below:g}{note}"
    )
    return Table(
        title=f"{rubric.title} · {rubric.name}",
        title_justify="left",
        title_style="bold",
        caption=caption,
        caption_justify="left",
        box=box.SIMPLE_HEAD,
    )


def _headline(result: DocumentResult, rubric: Rubric) -> tuple[Text, Text, Text]:
    """Status, composite, and verdict cells."""
    decision = result.decision
    assert decision is not None
    composite, policy = decision.composite, rubric.policy
    composite_style = (
        ""
        if composite is None
        else ("green" if composite >= policy.pass_at else "red" if composite < policy.fail_below else "yellow")
    )
    verdict = _verdict_text(result).replace(" (", " ").rstrip(")")
    return (
        Text(result.status, style=STATUS_STYLE[result.status]),
        Text(_composite_text(result), style=composite_style),
        Text(verdict, style=VERDICT_STYLE.get(decision.verdict or "", "")),
    )


def _dimension_ids(rubric: Rubric) -> list[str]:
    return [dim_id for dim_id in rubric.dimensions if dim_id != rubric.verdict_id]


def _cell_for(result: DocumentResult, dim_id: str) -> Text:
    assert result.decision is not None
    return _outcome_cell(next(o for o in result.decision.dimensions if o.id == dim_id))


def _matrix(rubric: Rubric, rows: Sequence[DocumentResult], names: dict[Path, str]) -> Table:
    table = _table(rubric)
    table.add_column("Document", overflow="fold", min_width=_name_width(names))
    table.add_column("Status")
    table.add_column("Score", justify="right")
    table.add_column("Verdict")
    for dim_id in _dimension_ids(rubric):
        dim = rubric.dimensions[dim_id]
        table.add_column(dim.label, justify="left" if dim.type == "choice" else "center")
    for result in rows:
        table.add_row(
            names[result.document.path],
            *_headline(result, rubric),
            *(_cell_for(result, dim_id) for dim_id in _dimension_ids(rubric)),
        )
    return table


def _short(name: str, limit: int = 24) -> str:
    return name if len(name) <= limit else f"{name[: limit - 9]}…{name[-8:]}"


def _transposed(rubric: Rubric, rows: Sequence[DocumentResult], names: dict[Path, str]) -> Table:
    table = _table(rubric)
    table.add_column("", no_wrap=True)
    for result in rows:
        table.add_column(_short(names[result.document.path]), justify="center")
    headlines = [_headline(result, rubric) for result in rows]
    for index, label in enumerate(("Status", "Score", "Verdict")):
        table.add_row(Text(label, style="bold"), *(headline[index] for headline in headlines), end_section=index == 2)
    for dim_id in _dimension_ids(rubric):
        table.add_row(rubric.dimensions[dim_id].label, *(_cell_for(result, dim_id) for result in rows))
    return table


def _compact(rubric: Rubric, rows: Sequence[DocumentResult], names: dict[Path, str]) -> Table:
    table = _table(rubric, " · use --details or a wider terminal for every dimension")
    table.add_column("Document", overflow="fold")
    table.add_column("Status")
    table.add_column("Score", justify="right")
    table.add_column("Verdict")
    table.add_column("Weakest", overflow="fold")
    for result in rows:
        assert result.decision is not None
        weakest = ", ".join(f"{o.label} {o.value:.1f}" for o in result.decision.weakest)
        table.add_row(names[result.document.path], *_headline(result, rubric), weakest)
    return table


def _bar(share: float, width: int = 10) -> str:
    filled = round(min(max(share, 0.0), 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def _details_panel(result: DocumentResult, name: str) -> Panel:
    decision, rubric = result.decision, result.rubric
    assert decision is not None and rubric is not None
    table = Table(box=None, pad_edge=False, show_header=True, header_style="dim")
    table.add_column("Dimension")
    table.add_column("Result")
    table.add_column("")
    table.add_column("Conf", justify="right")
    table.add_column("Most likely level", overflow="fold", ratio=1)
    for outcome in decision.dimensions:
        bar = (
            Text(_bar(outcome.normalized), style=_share_style(outcome.normalized))
            if outcome.normalized is not None
            else Text("")
        )
        confidence = f"{outcome.confidence:.2f}" if outcome.confidence is not None else ""
        table.add_row(outcome.label, _outcome_cell(outcome), bar, confidence, outcome.level or "")
    header = Text()
    if decision.composite is not None:
        header.append(f"Composite {points(decision.composite)}/100   ")
    if decision.verdict:
        header.append(f"Verdict {_verdict_text(result)}", style=VERDICT_STYLE.get(decision.verdict, ""))
    parts: list[Any] = [header, table]
    parts += [Text(f"• {reason}", style="dim") for reason in decision.reasons]
    notes = _input_notes(result.document)
    if notes:
        parts.append(Text(f"Input: {'; '.join(notes)}", style="dim"))
    title = Text(f"{name} · {rubric.title} · ")
    title.append(result.status.upper(), style=STATUS_STYLE[result.status])
    return Panel(Group(*parts), title=title, title_align="left")


# --- Dry run and listings --------------------------------------------------------------------------------------------


def request_body(request: PlannedRequest, model: str | None) -> dict[str, Any]:
    return {
        "state": request.state,
        "model": model or "(client default)",
        "questions": {name: question.model_dump(mode="json") for name, question in request.questions.items()},
    }


def dry_run_json(plans: Sequence[tuple[PreparedDocument, list[PlannedRequest]]], model: str | None) -> str:
    return json.dumps(
        [
            {
                "path": str(doc.path),
                "error": doc.error,
                "input": {
                    "chars": doc.original_chars,
                    "sent_chars": len(doc.text),
                    "truncated": doc.truncated,
                    "redactions": doc.redactions,
                },
                "requests": [
                    {
                        "purpose": request.purpose,
                        "conditional": request.conditional,
                        "body": request_body(request, model),
                    }
                    for request in requests
                ],
            }
            for doc, requests in plans
        ],
        indent=2,
        ensure_ascii=False,
    )


def render_dry_run(console: Console, plans: Sequence[tuple[PreparedDocument, list[PlannedRequest]]]) -> None:
    names = display_names([doc.path for doc, _ in plans])
    table = Table(title="Dry run: nothing was sent", title_justify="left", title_style="bold")
    table.add_column("Document", overflow="fold", min_width=_name_width(names))
    table.add_column("Chars sent", justify="right")
    table.add_column("Redactions")
    table.add_column("Requests", overflow="fold")
    table.add_column("≈ Input tokens", justify="right")
    for doc, requests in plans:
        if doc.error:
            table.add_row(names[doc.path], "—", "—", Text(f"error: {doc.error}", style="red"), "—")
            continue
        fixed = [request for request in requests if not request.conditional]
        routed = [request for request in requests if request.conditional]
        size = sum(len(json.dumps(request_body(request, None))) for request in fixed)
        size += max((len(json.dumps(request_body(request, None))) for request in routed), default=0)
        described = " → ".join(f"{request.purpose} ({len(request.questions)} q)" for request in fixed)
        if routed:
            counts = sorted({len(request.questions) for request in routed})
            span = f"{counts[0]}" if len(counts) == 1 else f"{counts[0]}-{counts[-1]}"
            described += f" → evaluate with the routed rubric ({span} q)"
        chars = f"{len(doc.text):,}" + (" (truncated)" if doc.truncated else "")
        redactions = ", ".join(f"{count} {kind}" for kind, count in sorted(doc.redactions.items())) or "none"
        table.add_row(names[doc.path], chars, redactions, described, f"{size // 4:,}")
    console.print(table)
    routed = any(request.conditional for _, requests in plans for request in requests)
    note = "Estimates use ~4 characters per token"
    note += " and count the largest rubric a document could be routed to." if routed else "."
    console.print(Text(f"{note} Use --format json to see exact request bodies.", style="dim"))


def render_presets(console: Console, rubrics: Sequence[Rubric]) -> None:
    table = Table(title="Built-in presets", title_justify="left", title_style="bold")
    table.add_column("Preset", style="bold")
    table.add_column("For")
    table.add_column("Dimensions", overflow="fold")
    for rubric in rubrics:
        dims = ", ".join(dim.label for dim in rubric.dimensions.values())
        table.add_row(rubric.name, rubric.description, dims)
    console.print(table)
    console.print(
        Text(
            "Default (no --preset): route each document to the best-fitting preset. "
            "Customize: doc-eval --show-rubric NAME > mine.yaml, edit, then --rubric mine.yaml",
            style="dim",
        )
    )
