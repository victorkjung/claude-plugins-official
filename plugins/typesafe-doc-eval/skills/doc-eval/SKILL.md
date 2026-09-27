---
name: doc-eval
description: Evaluate documents on quality, completeness, audience fit, tone, and readiness with TypeSafe's System One API, using the bundled doc-eval CLI. Use when the user wants to score, grade, review, compare, rank, or gate documents such as leases, rental applications, tenant notices, inspection reports, vendor contracts, investment or deal memos, underwriting, pitch-deck text, PRDs, specs, RFCs, runbooks, postmortems, marketing copy, listing descriptions, emails, or blog posts, or wants to write a custom evaluation rubric.
---

# Evaluate documents with doc-eval

`doc-eval` scores documents with TypeSafe typed judgments. Each rubric dimension is one TypeSafe
question (Score, Choice, or Noul). All of a document's questions go out in one batched request,
and code, not the model, turns the answers into a composite score (0-100) and a status.

## Run it

The CLI ships with this plugin. Run it with uv; nothing needs installing first:

```sh
uvx --from "${CLAUDE_SKILL_DIR}/../.." doc-eval <files or directories> [options]
```

It needs `TYPESAFE_API_KEY` in the environment. Never ask the user to paste a key into the chat.
If the key is missing, tell them to export it (or add it to their environment's secrets), and offer
`--dry-run` (shows the exact requests, sends nothing) or `--mock` (synthetic answers that preview
the output format; they are not evaluations, so never present them as results).

Pick a preset, or omit `--preset` to route each document to the best fit automatically:

| Documents | Preset |
|---|---|
| Leases, rental applications, inspection and maintenance reports, tenant notices, vendor contracts | `property` |
| Investment memos, underwriting, pitch-deck text, market analyses | `deal-memo` |
| PRDs, specs, RFCs and design docs, runbooks, postmortems | `product-spec` |
| Marketing copy, listing descriptions, emails and newsletters, blog posts | `outbound` |

Useful options: `--audience "..."` (who the documents are for), `--context "..."` (background such
as jurisdiction or deal type), `-f json` (results to reason over), `-f markdown -o report.md`
(shareable report), `--details` (per-dimension terminal breakdown), `--fail-on revise|review|fail`
(exit 1 for CI gates), and `--pass-at` / `--fail-below` (thresholds). Run `doc-eval --help` for the rest.

## Read the results

Use `-f json` when you need to analyze results yourself. Each document has `status`, `composite`,
`verdict` (`label`, `confidence`), `reasons`, `weakest` (dimension ids), and `dimensions`; each
dimension has its answer, `confidence`, and `level`, the rubric description of the most likely
level. `input` shows truncation and redaction counts; `error` explains documents that could not be
evaluated.

| Status | Meaning |
|---|---|
| pass | Verdict approve and composite at or above the pass bar (75 by default) |
| revise | Fixable: verdict revise, or composite between the fail and pass bars |
| review | A person should decide: conflicting or low-confidence signals, a flagged answer, a truncated document, or one that may not fit the rubric |
| fail | Verdict reject, or composite below the fail bar (50 by default) |
| error | Not evaluated (unreadable file, missing key, API error) |

When you report back, lead with the status counts. Then, for each document that did not pass, give
the reasons and the weakest dimensions with their `level` text, and propose specific edits drawn
from the document itself. These are calibrated model judgments, not ground truth: point out low
confidence, and recommend a human look at `review` documents. Exit codes: 0 ok, 1 `--fail-on`
tripped, 2 usage or rubric error, 3 a document could not be evaluated.

## Custom rubrics

Start from a preset (`doc-eval --show-rubric property > my-rubric.yaml`) or from
`${CLAUDE_SKILL_DIR}/../../examples/rubrics/owner-report.yaml`, then run it with
`--rubric my-rubric.yaml`. Follow TypeSafe's question-design guidance:

- Ask one narrow judgment per dimension. Dimension ids are not sent to the model, so the
  instructions must carry the full meaning and reference state by path: `document.text`,
  `evaluation_context.intended_audience`, `evaluation_context.expected_document_kind`, and
  `evaluation_context.background` (from `--context`).
- `score`: ordered `criteria`, one per level starting at 0. Five levels work well; each level must
  describe a concrete situation that stands on its own. `weight` sets its share of the composite.
- `choice`: a label-to-description map. The verdict dimension needs `role: verdict` and exactly the
  labels `approve`, `revise`, and `reject`. Include a no-match label when nothing may fit.
- `noul`: a yes/no condition. A `weight` counts P(yes) in the composite.
- `flag` escalates a status: `labels` for choices, `when: yes|no` (with an optional `threshold`)
  for nouls, `below` for scores, and `action: review` (default) or `fail`.
- Quote YAML labels such as `'yes'` and `'no'`, which YAML otherwise reads as booleans.
- Check the result with `--dry-run -f json` before spending tokens.

For deeper TypeSafe design guidance, see the official TypeSafe skill at
https://github.com/typesafe-ai/skills and the docs at https://docs.typesafe.ai.

## Data handling

Document text is sent to api.typesafe.ai. By default, emails, phone numbers, SSNs, card numbers,
and API secrets are replaced with placeholders such as `[EMAIL]` before sending (`--no-redact`
turns this off). Documents longer than `--max-chars` (default 120,000) keep their head and tail,
and the truncated document is sent to review.
