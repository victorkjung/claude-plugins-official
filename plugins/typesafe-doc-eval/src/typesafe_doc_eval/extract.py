"""Turn input files into the text that is sent as state: extract, normalize, redact, truncate."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".text", ".rst"})
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | {".pdf", ".docx"}


class ExtractionError(Exception):
    """A file's text could not be extracted."""


@dataclass
class PreparedDocument:
    path: Path
    text: str = ""
    original_chars: int = 0
    truncated: bool = False
    redactions: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def collect_paths(inputs: list[str]) -> tuple[list[Path], list[str]]:
    """Expand files and directories (recursively, supported types only) into an ordered, de-duplicated list."""
    paths: list[Path] = []
    missing: list[str] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            paths += sorted(
                child
                for child in path.rglob("*")
                if child.is_file()
                and child.suffix.lower() in SUPPORTED_SUFFIXES
                and not any(part.startswith(".") for part in child.relative_to(path).parts)
            )
        elif path.is_file():
            paths.append(path)
        else:
            missing.append(raw)
    return list(dict.fromkeys(paths)), missing


def prepare(path: Path, *, max_chars: int, redact: bool) -> PreparedDocument:
    doc = PreparedDocument(path=path)
    try:
        text = normalize(extract_text(path))
    except ExtractionError as error:
        doc.error = str(error)
        return doc
    if not text.strip():
        doc.error = "no extractable text (a scanned PDF needs OCR first)"
        return doc
    doc.original_chars = len(text)
    if redact:
        text, doc.redactions = redact_text(text)
    doc.text, doc.truncated = truncate(text, max_chars)
    return doc


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_text(path)
    if suffix == ".docx":
        return _docx_text(path)
    if suffix in TEXT_SUFFIXES:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise ExtractionError(f"cannot read file: {error.strerror or error}") from error
    supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
    raise ExtractionError(f"unsupported file type {suffix or '(no extension)'}; supported: {supported}")


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractionError("PDF is password-protected")
        return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages)
    except ExtractionError:
        raise
    except Exception as error:  # third-party parser on arbitrary input: report per document, don't crash the run
        raise ExtractionError(f"cannot read PDF: {error or type(error).__name__}") from error


def _docx_text(path: Path) -> str:
    import docx
    from docx.table import Table

    try:
        document = docx.Document(str(path))
    except Exception as error:  # third-party parser on arbitrary input: report per document, don't crash the run
        raise ExtractionError(f"cannot read DOCX: {error or type(error).__name__}") from error
    lines: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                cells = [cell.text.strip() for cell in row.cells]
                # Merged cells repeat their text once per spanned column.
                deduped = [text for i, text in enumerate(cells) if i == 0 or text != cells[i - 1]]
                lines.append(" | ".join(deduped))
            lines.append("")
            continue
        text = block.text.strip()
        style = (block.style.name if block.style is not None else "") or ""
        heading = re.match(r"Heading (\d)", style)
        if heading and text:
            text = "#" * int(heading.group(1)) + " " + text
        elif style.startswith("List") and text:
            text = "- " + text
        lines.append(text)
    return "\n".join(lines)


def normalize(text: str) -> str:
    """Trim trailing spaces and runs of blank lines, which cost tokens without adding meaning."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Keep the head and tail (summaries open documents; asks and signatures close them)."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    head = int(max_chars * 0.7)
    tail = max_chars - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n[... {omitted:,} characters omitted for length ...]\n\n{text[len(text) - tail :]}", True


# --- Redaction -------------------------------------------------------------------------------------------------------

_SECRET = re.compile(
    r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant)[-_][A-Za-z0-9_-]{16,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}\b"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*"
)
_SECRET_FIELD = re.compile(r"(?i)\b(password|passwd|api[_ -]?key|secret|access[_ -]?token)(\s*[:=]\s*)\S+")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_PHONE = re.compile(r"(?<![\w-])(?:\+?1[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])")


def _luhn(number: str) -> bool:
    digits = [int(char) for char in number if char.isdigit()]
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        checksum += digit
    return checksum % 10 == 0


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Mask secrets and personal data with typed placeholders; return the text and per-kind counts."""
    counts: Counter[str] = Counter()

    def sub(kind: str, pattern: re.Pattern[str], replacement: str, text: str) -> str:
        text, count = pattern.subn(replacement, text)
        counts[kind] += count
        return text

    text = sub("secret", _SECRET, "[SECRET]", text)
    text = sub("secret", _SECRET_FIELD, r"\1\2[SECRET]", text)
    text = sub("email", _EMAIL, "[EMAIL]", text)
    text = sub("ssn", _SSN, "[SSN]", text)

    def card(match: re.Match[str]) -> str:
        if _luhn(match.group()):
            counts["card"] += 1
            return "[CARD]"
        return match.group()

    text = _CARD.sub(card, text)
    text = sub("phone", _PHONE, "[PHONE]", text)
    return text, {kind: count for kind, count in counts.items() if count}
