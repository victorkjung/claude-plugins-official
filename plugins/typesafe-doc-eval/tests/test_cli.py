import json
from pathlib import Path

import httpx2
import pytest
import yaml
from conftest import ScriptedAPI, choice_json, score_json

from typesafe_doc_eval import cli
from typesafe_doc_eval.rubric import KIND_CHECK_ID, NO_MATCH, ROUTE_ID, load_preset

EXAMPLES = Path(__file__).parent.parent / "examples" / "docs"


def run_json(capsys, *args: str) -> tuple[int, dict]:
    code = cli.main([*args, "--format", "json"])
    return code, json.loads(capsys.readouterr().out)


def test_single_preset_sends_one_batched_request_with_named_state(api: ScriptedAPI, lease, capsys):
    code, report = run_json(capsys, str(lease), "-p", "property", "--audience", "NY tenants", "--context", "NYC lease")
    assert code == 0
    assert len(api.requests) == 1
    body = api.requests[0]
    assert set(body["questions"]) == {*load_preset("property").dimensions, KIND_CHECK_ID}
    state = body["state"]
    assert "[EMAIL]" in state["document"]["text"] and "jordan@example.com" not in state["document"]["text"]
    assert "[PHONE]" in state["document"]["text"]
    assert state["evaluation_context"]["intended_audience"] == "NY tenants"
    assert state["evaluation_context"]["background"] == "NYC lease"
    assert state["document"]["notes"]  # tells the model placeholders stand for present values

    [doc] = report["documents"]
    assert doc["status"] == "pass" and doc["rubric"] == "property"
    assert doc["verdict"]["label"] == "approve"
    assert doc["input"]["redactions"] == {"email": 1, "phone": 1}
    assert doc["dimensions"]["clarity"]["max"] == 4
    assert report["summary"]["pass"] == 1 and report["mock"] is False


def test_auto_routes_then_evaluates_with_the_chosen_rubric(api: ScriptedAPI, lease, capsys):
    def answer(name, question, body):
        if name == ROUTE_ID:
            return choice_json("deal-memo", list(question["criteria"]), 0.93)
        return api_default(name, question, body)

    api_default, api.answer = api.answer, answer
    code, report = run_json(capsys, str(lease))
    assert code == 0
    route, evaluation = api.requests
    assert set(route["questions"]) == {ROUTE_ID}
    assert list(route["questions"][ROUTE_ID]["criteria"]) == [
        "deal-memo",
        "outbound",
        "product-spec",
        "property",
        NO_MATCH,
    ]
    assert "document_excerpt" in route["state"]
    assert set(evaluation["questions"]) == set(load_preset("deal-memo").dimensions)  # no kind check when routed
    [doc] = report["documents"]
    assert doc["rubric"] == "deal-memo" and doc["routing"]["confidence"] == 0.93


def test_unmatched_document_is_sent_to_review_without_scoring(api: ScriptedAPI, lease, capsys):
    api.answer = lambda name, question, body: choice_json(NO_MATCH, list(question["criteria"]), 0.8)
    code, report = run_json(capsys, str(lease), "--fail-on", "review")
    assert code == 1
    assert len(api.requests) == 1
    [doc] = report["documents"]
    assert doc["status"] == "review" and doc["rubric"] is None
    assert "No rubric fits" in doc["reasons"][0]


def test_fail_on_controls_the_exit_code(api: ScriptedAPI, lease, capsys):
    def middling(name, question, body):
        if question["type"] == "score":
            return score_json(2.6)
        if question["type"] == "noul":
            return {"type": "noul", "noul": 0.9}
        labels = list(question["criteria"])
        return choice_json("revise" if "revise" in labels else labels[0], labels)

    api.answer = middling
    assert run_json(capsys, str(lease), "-p", "property")[0] == 0
    code, report = run_json(capsys, str(lease), "-p", "property", "--fail-on", "revise")
    assert code == 1 and report["documents"][0]["status"] == "revise"
    assert run_json(capsys, str(lease), "-p", "property", "--pass-at", "60", "--fail-on", "revise")[0] == 1
    assert run_json(capsys, str(lease), "-p", "property", "--fail-on", "never")[0] == 0


def test_api_errors_are_reported_per_document_with_exit_3(api: ScriptedAPI, lease, capsys):
    api.status = 401
    code, report = run_json(capsys, str(lease), "-p", "property")
    assert code == 3
    [doc] = report["documents"]
    assert doc["status"] == "error" and "TYPESAFE_API_KEY" in doc["error"]


def test_unreadable_document_does_not_stop_the_run(api: ScriptedAPI, lease, tmp_path, capsys):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    code, report = run_json(capsys, str(lease), str(broken), "-p", "property")
    assert code == 3
    statuses = {Path(doc["path"]).name: doc["status"] for doc in report["documents"]}
    assert statuses == {"lease.md": "pass", "broken.pdf": "error"}
    assert len(api.requests) == 1


def test_dry_run_sends_nothing_and_shows_every_candidate_request(api: ScriptedAPI, lease, capsys):
    code = cli.main([str(lease), "--dry-run", "--format", "json"])
    assert code == 0 and api.requests == []
    [plan] = json.loads(capsys.readouterr().out)
    purposes = [(request["purpose"], request["conditional"]) for request in plan["requests"]]
    assert purposes[0] == ("route", False)
    assert {purpose for purpose, conditional in purposes[1:] if conditional} == {
        "evaluate:deal-memo",
        "evaluate:outbound",
        "evaluate:product-spec",
        "evaluate:property",
    }


def test_dry_run_table_needs_no_api_key(lease, capsys, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert cli.main([str(lease), "-p", "property", "--dry-run"]) == 0
    assert "nothing was sent" in capsys.readouterr().out


def test_missing_api_key_is_a_usage_error(lease, capsys, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert cli.main([str(lease), "-p", "property"]) == 2
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "no documents given"),
        (["missing.md"], "not found: missing.md"),
        (["-p", "nope", "x"], "unknown preset"),
        (["--pass-at", "40", "--fail-below", "60", "x"], "invalid --pass-at/--fail-below"),
        (["--concurrency", "0", "x"], "--concurrency"),
        (["--max-chars", "-1", "x"], "--max-chars"),
    ],
)
def test_usage_errors(args, message, capsys):
    assert cli.main(args) == 2
    assert message in capsys.readouterr().err


def test_mock_mode_runs_offline_and_is_labeled(capsys, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    code, report = run_json(capsys, str(EXAMPLES), "--mock")
    assert report["mock"] is True and report["summary"]["documents"] == 4
    assert {doc["rubric"] for doc in report["documents"]} == {"property", "deal-memo", "product-spec", "outbound"}
    statuses = [doc["status"] for doc in report["documents"]]
    assert code == (1 if "fail" in statuses else 0)
    assert cli.main([str(EXAMPLES), "--mock", "--format", "markdown", "--fail-on", "never"]) == 0
    markdown = capsys.readouterr().out
    assert "MOCK MODE" in markdown and "| Document | Rubric | Status |" in markdown


def test_table_output_and_details(api: ScriptedAPI, lease, capsys):
    assert cli.main([str(lease), "-p", "property", "--details"]) == 0
    out = capsys.readouterr().out
    assert "Property & leasing docs" in out and "lease.md" in out and "Most likely level" in out
    assert "1 document: 1 pass" in out


def test_out_file_and_markdown(api: ScriptedAPI, lease, tmp_path, capsys):
    target = tmp_path / "report.md"
    assert cli.main([str(lease), "-p", "property", "-f", "markdown", "-o", str(target)]) == 0
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Document evaluation") and "## " in text and "| Clarity |" in text
    assert capsys.readouterr().out == ""


def test_list_and_show_rubric(capsys):
    assert cli.main(["--list-presets"]) == 0
    listing = capsys.readouterr().out
    assert all(name in listing for name in ("property", "deal-memo", "product-spec", "outbound"))
    assert cli.main(["--show-rubric", "property"]) == 0
    assert yaml.safe_load(capsys.readouterr().out)["name"] == "property"


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (429, None, "rate limited after retries; lower --concurrency"),
        (200, httpx2.ReadTimeout("slow"), "request timed out; raise --timeout"),
        (200, httpx2.ConnectError("refused"), "cannot reach the TypeSafe API"),
    ],
)
def test_transport_failures_explain_the_fix(api: ScriptedAPI, lease, capsys, status, error, message):
    api.status, api.raise_error = status, error
    code, report = run_json(capsys, str(lease), "-p", "property")
    assert code == 3 and message in report["documents"][0]["error"]


def test_answer_outside_the_rubric_labels_is_an_error(api: ScriptedAPI, lease, capsys):
    def off_script(name, question, body):
        if name == "tone":
            return choice_json("sarcastic", ["sarcastic", "professional"])
        return api_default(name, question, body)

    api_default, api.answer = api.answer, off_script
    code, report = run_json(capsys, str(lease), "-p", "property")
    assert code == 3 and "not one of the rubric's labels" in report["documents"][0]["error"]


def test_unwritable_output_fails_before_calling_the_api(api: ScriptedAPI, lease, tmp_path, capsys):
    assert cli.main([str(lease), "-p", "property", "-o", str(tmp_path / "missing" / "report.md")]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert api.requests == []
