import time
from pathlib import Path

import docx
import pytest

from typesafe_doc_eval.extract import (
    ExtractionError,
    collect_paths,
    extract_text,
    normalize,
    prepare,
    redact_text,
    truncate,
)


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF with a text layer, with a correct xref table."""
    stream = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_collect_paths_expands_directories_and_reports_missing(tmp_path: Path):
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "notes.xlsx").write_text("skip me")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "c.md").write_text("c")

    paths, missing = collect_paths([str(tmp_path), str(tmp_path / "a.md"), "nope.md"])
    assert paths == [tmp_path / "a.md", tmp_path / "sub" / "b.txt"]
    assert missing == ["nope.md"]


def test_explicit_unsupported_file_becomes_a_per_document_error(tmp_path: Path):
    path = tmp_path / "sheet.xlsx"
    path.write_bytes(b"PK")
    doc = prepare(path, max_chars=1000, redact=True)
    assert doc.error is not None and "unsupported file type .xlsx" in doc.error


def test_empty_document_is_an_error(tmp_path: Path):
    path = tmp_path / "blank.md"
    path.write_text("   \n\n  ")
    assert prepare(path, max_chars=1000, redact=True).error == "no extractable text (a scanned PDF needs OCR first)"


def test_pdf_text_is_extracted(tmp_path: Path):
    path = tmp_path / "lease.pdf"
    path.write_bytes(minimal_pdf("Rent is due on the first"))
    assert "Rent is due on the first" in extract_text(path)


def test_corrupt_pdf_raises_extraction_error(tmp_path: Path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(ExtractionError):
        extract_text(path)


def test_docx_keeps_headings_lists_and_tables_in_order(tmp_path: Path):
    document = docx.Document()
    document.add_heading("Lease Terms", level=1)
    document.add_paragraph("Rent is $2,850 per month.")
    document.add_paragraph("Water and trash", style="List Bullet")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Item", "Amount"
    table.cell(1, 0).text, table.cell(1, 1).text = "Deposit", "$2,850"
    document.add_paragraph("Signed below.")
    path = tmp_path / "lease.docx"
    document.save(str(path))

    text = extract_text(path)
    assert text.index("# Lease Terms") < text.index("- Water and trash") < text.index("Deposit | $2,850")
    assert text.index("Deposit | $2,850") < text.index("Signed below.")


def test_normalize_and_truncate():
    assert normalize("a  \r\n\n\n\nb\t\n") == "a\n\nb"
    text = "H" * 70 + "M" * 1000 + "T" * 30
    cut, truncated = truncate(text, 100)
    assert truncated and cut.startswith("H" * 70) and cut.endswith("T" * 30)
    assert "M" not in cut and cut.count("H") == 70 and cut.count("T") == 30
    assert "1,000 characters omitted" in cut
    assert truncate(text, 0) == (text, False)
    assert truncate("short", 100) == ("short", False)


def test_redaction_masks_personal_data_and_secrets_but_not_business_facts():
    text = (
        "Contact jordan@example.com or (212) 555-0100 / +1 646.555.0199. SSN 123-45-6789. "
        "Card 4111 1111 1111 1111. Not a card: 1234 5678 9012 3456. "
        "Key sk-proj-abcdefghijklmnopqrstuv and password: hunter2. "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123. "
        "Rent $2,850 due 2026-10-01; unit 4B; account total 1,250,000; zip 11201."
    )
    redacted, counts = redact_text(text)
    for secret in (
        "jordan@example.com",
        "555-0100",
        "555.0199",
        "123-45-6789",
        "4111 1111",
        "sk-proj",
        "hunter2",
        "abcdefghijklmnopqrstuvwxyz123",
    ):
        assert secret not in redacted, secret
    for fact in ("$2,850", "2026-10-01", "4B", "1,250,000", "11201", "1234 5678 9012 3456"):
        assert fact in redacted, fact
    assert counts == {"email": 1, "phone": 2, "ssn": 1, "card": 1, "secret": 3}


def test_prepare_redacts_before_truncating(tmp_path: Path):
    path = tmp_path / "app.md"
    path.write_text("Applicant: jordan@example.com\n" + "x" * 500)
    doc = prepare(path, max_chars=100, redact=True)
    assert doc.redactions == {"email": 1} and doc.truncated
    assert "[EMAIL]" in doc.text and doc.original_chars > 500
    assert "jordan@example.com" in prepare(path, max_chars=0, redact=False).text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Leaks the first redactor missed.
        ("Deposit card: 4111 1111 1111 1111 12/28", "Deposit card: [CARD] 12/28"),
        ("Applicant SSN 123456789.", "Applicant SSN [SSN]."),
        ("Social Security Number: 123 45 6789", "Social Security Number: [SSN]"),
        ("Phone: 2125550100 / Cell 212 555 0100", "Phone: [PHONE] / Cell [PHONE]"),
        (
            "DB_PASSWORD=hunter2\nAWS_SECRET_ACCESS_KEY=abc123xyz789",
            "DB_PASSWORD=[SECRET]\nAWS_SECRET_ACCESS_KEY=[SECRET]",
        ),
        ('{"api_key": "abc123xyz", "client_secret": "s3cr3t"}', '{"api_key": "[SECRET]", "client_secret": "[SECRET]"}'),
        ("key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\nend", "key:\n[SECRET]\nend"),
        # Business facts the first redactor corrupted or might.
        ("Lease term 2026-02-01 2027-01-31", "Lease term 2026-02-01 2027-01-31"),
        ("Unit 204 950 2400", "Unit 204 950 2400"),
        ("Our secret: location, location.", "Our secret: location, location."),
        ("secret:\nnext line", "secret:\nnext line"),
        ("Ref 4111 1111 1111 1112", "Ref 4111 1111 1111 1112"),
        ("Rent $2,850; total 1,250,000; zip 11201-1234", "Rent $2,850; total 1,250,000; zip 11201-1234"),
    ],
)
def test_redaction_cases(text, expected):
    assert redact_text(text)[0] == expected


def test_docx_keeps_equal_neighbours_but_collapses_merged_cells(tmp_path: Path):
    document = docx.Document()
    table = document.add_table(rows=2, cols=3)
    for column, text in enumerate(("Kitchen", "Good", "Good")):
        table.cell(0, column).text = text
    merged = table.cell(1, 1).merge(table.cell(1, 2))
    table.cell(1, 0).text, merged.text = "Deposit", "$2,850"
    path = tmp_path / "inspection.docx"
    document.save(str(path))
    assert extract_text(path).splitlines()[:2] == ["Kitchen | Good | Good", "Deposit | $2,850"]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("Lease for Unit 4B.".encode("utf-16"), "Lease for Unit 4B."),
        (b"\xef\xbb\xbfLease", "Lease"),
        ("Café lease".encode("cp1252"), "Café lease"),
        ("Café lease".encode(), "Café lease"),
    ],
)
def test_text_decoding(tmp_path: Path, data: bytes, expected: str):
    path = tmp_path / "doc.txt"
    path.write_bytes(data)
    assert extract_text(path) == expected


def test_nul_heavy_text_without_bom_is_an_error(tmp_path: Path):
    path = tmp_path / "doc.txt"
    path.write_bytes("Lease for Unit 4B.".encode("utf-16-le"))
    with pytest.raises(ExtractionError, match="UTF-16 without a byte-order mark"):
        extract_text(path)


def test_same_file_given_twice_is_collected_once(tmp_path: Path, monkeypatch):
    (tmp_path / "a.md").write_text("a")
    monkeypatch.chdir(tmp_path)
    paths, _ = collect_paths(["a.md", str(tmp_path / "a.md"), "."])
    assert paths == [Path("a.md")]


@pytest.mark.parametrize("text", ["password-" * 13_000, "a-" * 60_000, "a." * 60_000, "k: v " * 24_000])
def test_redaction_stays_linear_on_pathological_input(text):
    # These took minutes when a pattern backtracked; linear scanning takes well under 100 ms.
    start = time.perf_counter()
    redact_text(text)
    assert time.perf_counter() - start < 2.0
