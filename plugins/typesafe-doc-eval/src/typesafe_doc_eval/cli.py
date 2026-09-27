"""doc-eval: evaluate documents on multiple dimensions with TypeSafe's System One API."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import TextIO

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from typesafe_sdk import AsyncTypeSafeClient, TypeSafeError

from typesafe_doc_eval import __version__, mock
from typesafe_doc_eval.evaluate import DocumentResult, Options, evaluate_all, plan
from typesafe_doc_eval.extract import SUPPORTED_SUFFIXES, collect_paths, prepare
from typesafe_doc_eval.policy import SEVERITY
from typesafe_doc_eval.report import (
    RunInfo,
    dry_run_json,
    render_dry_run,
    render_presets,
    render_table,
    to_json,
    to_markdown,
)
from typesafe_doc_eval.rubric import RubricError, load_preset, preset_names, preset_text, resolve_rubrics

EXIT_OK, EXIT_GATE, EXIT_USAGE, EXIT_ERRORS = 0, 1, 2, 3

EPILOG = f"""\
examples:
  doc-eval docs/                          route each file to the best-fitting preset
  doc-eval lease.pdf --preset property    score against one preset
  doc-eval memos/ -p deal-memo --audience "LPs in a real estate fund" -f markdown -o report.md
  doc-eval specs/*.md -p product-spec --fail-on revise    CI gate: exit 1 unless every doc passes
  doc-eval --show-rubric outbound > mine.yaml   start a custom rubric, then: --rubric mine.yaml

supported files: {", ".join(sorted(SUPPORTED_SUFFIXES))}
environment: TYPESAFE_API_KEY (required), TYPESAFE_DEFAULT_MODEL, TYPESAFE_BASE_URL
exit codes: 0 ok · 1 a document tripped --fail-on · 2 usage or rubric error · 3 a document could not be evaluated
"""


class UsageError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doc-eval",
        description="Score documents on quality, audience fit, tone, and readiness with TypeSafe's System One API.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", help="files or directories to evaluate")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    rubrics = parser.add_argument_group("rubrics")
    rubrics.add_argument(
        "-p",
        "--preset",
        action="append",
        default=[],
        metavar="NAME",
        help="built-in rubric; repeat or comma-separate to route among several (default: all, routed)",
    )
    rubrics.add_argument(
        "-r", "--rubric", action="append", default=[], type=Path, metavar="FILE", help="custom rubric YAML; repeatable"
    )
    rubrics.add_argument("--list-presets", action="store_true", help="list built-in presets and exit")
    rubrics.add_argument("--show-rubric", metavar="NAME|FILE", help="print a rubric's YAML and exit")

    context = parser.add_argument_group("evaluation context")
    context.add_argument("--audience", help="who the documents are for (overrides the rubric's default audience)")
    context.add_argument("--context", help="background the model should know, e.g. 'NY residential leases'")
    context.add_argument("--model", help="TypeSafe model (default: $TYPESAFE_DEFAULT_MODEL or jev-latest)")

    inputs = parser.add_argument_group("input handling")
    inputs.add_argument(
        "--max-chars",
        type=int,
        default=120_000,
        metavar="N",
        help="truncate longer documents, keeping head and tail (default: 120000; 0 = never)",
    )
    inputs.add_argument(
        "--no-redact",
        dest="redact",
        action="store_false",
        help="send text without masking emails, phones, SSNs, card numbers, and secrets",
    )
    inputs.add_argument(
        "--no-kind-check",
        dest="kind_check",
        action="store_false",
        help="skip the check that a document is the kind its single rubric covers",
    )

    output = parser.add_argument_group("output and gating")
    output.add_argument("-f", "--format", choices=("table", "json", "markdown"), default="table")
    output.add_argument("-o", "--out", type=Path, metavar="FILE", help="write the report to FILE instead of stdout")
    output.add_argument(
        "--details",
        action="store_true",
        help="table format: add a per-document breakdown with confidence and level descriptions",
    )
    output.add_argument(
        "--fail-on",
        choices=("fail", "review", "revise", "never"),
        default="fail",
        help="exit 1 when any document's status is at least this severe (default: fail)",
    )
    output.add_argument("--pass-at", type=float, metavar="N", help="override every rubric's pass bar (0-100)")
    output.add_argument("--fail-below", type=float, metavar="N", help="override every rubric's fail bar (0-100)")

    execution = parser.add_argument_group("execution")
    execution.add_argument(
        "-j", "--concurrency", type=int, default=4, metavar="N", help="documents evaluated in parallel (default: 4)"
    )
    execution.add_argument(
        "--timeout", type=float, default=60.0, metavar="SECONDS", help="per-request timeout (default: 60)"
    )
    execution.add_argument(
        "--dry-run", action="store_true", help="prepare documents and show the requests without calling the API"
    )
    execution.add_argument(
        "--mock",
        action="store_true",
        help="use synthetic answers (no API calls) to preview output; not real evaluations",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (UsageError, RubricError) as error:
        print(f"doc-eval: error: {error}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130


def run(args: argparse.Namespace) -> int:
    stdout = Console(highlight=False, soft_wrap=False)
    if args.list_presets:
        render_presets(stdout, [load_preset(name) for name in preset_names()])
        return EXIT_OK
    if args.show_rubric:
        sys.stdout.write(_rubric_source(args.show_rubric))
        return EXIT_OK

    if args.concurrency < 1:
        raise UsageError("--concurrency must be at least 1")
    if args.timeout <= 0:
        raise UsageError("--timeout must be positive")
    if args.max_chars < 0:
        raise UsageError("--max-chars must be 0 (never truncate) or more")
    if args.out is not None:
        # Fail before spending tokens, not after.
        parent = args.out.parent if str(args.out.parent) else Path(".")
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            raise UsageError(f"cannot write {args.out}: directory {parent} does not exist or is not writable")
    rubrics = resolve_rubrics(args.preset, args.rubric)
    overrides = {
        key: value for key, value in (("pass_at", args.pass_at), ("fail_below", args.fail_below)) if value is not None
    }
    if overrides:
        try:
            rubrics = [rubric.with_policy(**overrides) for rubric in rubrics]
        except ValueError as error:
            raise UsageError(f"invalid --pass-at/--fail-below: {error}") from error

    if not args.paths:
        raise UsageError("no documents given (see --help)")
    paths, missing = collect_paths(args.paths)
    if missing:
        raise UsageError(f"not found: {', '.join(missing)}")
    if not paths:
        raise UsageError(f"no supported documents found ({', '.join(sorted(SUPPORTED_SUFFIXES))})")

    docs = [prepare(path, max_chars=args.max_chars, redact=args.redact) for path in paths]
    options = Options(
        model=args.model,
        audience=args.audience,
        context=args.context,
        kind_check=args.kind_check,
        concurrency=args.concurrency,
    )
    info = RunInfo(rubrics=rubrics, mock=args.mock)

    if args.dry_run:
        plans = [(doc, [] if doc.error else plan(doc, rubrics, options)) for doc in docs]
        with _output(args.out) as out:
            if args.format == "json":
                out.write(dry_run_json(plans, args.model) + "\n")
            else:
                render_dry_run(_console(out, args.out), plans)
        return EXIT_OK

    results = asyncio.run(_evaluate(docs, rubrics, options, args))
    with _output(args.out) as out:
        if args.format == "json":
            out.write(to_json(results, info) + "\n")
        elif args.format == "markdown":
            out.write(to_markdown(results, info))
        else:
            render_table(_console(out, args.out), results, info, details=args.details)
    if args.out:
        print(f"doc-eval: wrote {args.out}", file=sys.stderr)
    return exit_code(results, args.fail_on)


async def _evaluate(docs: list, rubrics: list, options: Options, args: argparse.Namespace) -> list[DocumentResult]:
    try:
        client = AsyncTypeSafeClient(
            api_key="mock" if args.mock else None,
            model=args.model,
            timeout=args.timeout,
            transport=mock.transport() if args.mock else None,
        )
    except TypeSafeError as error:
        raise UsageError(f"{error} Get a key at https://typesafe.ai, or preview output with --mock.") from error
    stderr = Console(stderr=True)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("Evaluating"),
        BarColumn(),
        MofNCompleteColumn(),
        console=stderr,
        transient=True,
        disable=not stderr.is_terminal,
    )
    async with client:
        with progress:
            task = progress.add_task("evaluate", total=len(docs))
            return await evaluate_all(client, docs, rubrics, options, on_done=lambda _: progress.advance(task))


def exit_code(results: Sequence[DocumentResult], fail_on: str) -> int:
    if any(result.status == "error" for result in results):
        return EXIT_ERRORS
    if fail_on != "never" and any(SEVERITY[result.status] >= SEVERITY[fail_on] for result in results):
        return EXIT_GATE
    return EXIT_OK


def _rubric_source(name_or_path: str) -> str:
    if name_or_path in preset_names():
        return preset_text(name_or_path)
    path = Path(name_or_path)
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise UsageError(f"cannot read {path}: {error}") from error
    raise UsageError(f"{name_or_path!r} is neither a preset ({', '.join(preset_names())}) nor a file")


def _output(path: Path | None):
    if path is None:
        return nullcontext(sys.stdout)
    try:
        return path.open("w", encoding="utf-8")
    except OSError as error:
        raise UsageError(f"cannot write {path}: {error.strerror or error}") from error


def _console(out: TextIO, path: Path | None) -> Console:
    console = Console(file=out, highlight=False, no_color=path is not None)
    if not console.is_terminal:
        console.width = 160  # files and pipes: wide enough for the score matrix
    return console
