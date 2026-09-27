"""Turn input files into the text that is sent as state: extract, normalize, redact, truncate."""

from __future__ import annotations

import codecs
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
    seen: set[Path] = set()
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
    unique = []
    for path in paths:  # the same file given twice (e.g. relative and absolute) is evaluated and billed once
        if path.resolve() not in seen:
            seen.add(path.resolve())
            unique.append(path)
    return unique, missing


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
            return _decode(path.read_bytes())
        except OSError as error:
            raise ExtractionError(f"cannot read file: {error.strerror or error}") from error
    supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
    raise ExtractionError(f"unsupported file type {suffix or '(no extension)'}; supported: {supported}")


_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _decode(data: bytes) -> str:
    """Honor byte-order marks; otherwise UTF-8, then Windows-1252 (common for .txt saved on Windows), then Latin-1."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding, errors="replace")
    if data.count(0) > len(data) // 10:
        raise ExtractionError("looks like a binary file or UTF-16 without a byte-order mark; save it as UTF-8")
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


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
                cells = list(row.cells)
                # A merged cell appears once per spanned column, as the same underlying element.
                unique = [cell for i, cell in enumerate(cells) if i == 0 or cell._tc is not cells[i - 1]._tc]
                lines.append(" | ".join(cell.text.strip() for cell in unique))
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

_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
_TOKEN = re.compile(  # well-known credential formats
    r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant)[-_][A-Za-z0-9_-]{16,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}\b"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*"
)
# `password: x`, `DB_PASSWORD=x`, `"api_key": "x"`: any `name: value` field (value on the same line) whose name
# says it holds a credential. Patterns start only where a token begins (lookbehind, not \b) so that long runs of
# word characters are scanned once; Python 3.10 has no possessive quantifiers to prevent the backtracking.
_FIELD = re.compile(r"(?<![\w-])([\w-]+)([\"']?[ \t]*[:=][ \t]*[\"']?)([^\s\"',;]+)")
_CREDENTIAL_NAME = re.compile(
    r"(?i)password|passwd|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|secret[_-]?key"
    r"|private[_-]?key|[a-z0-9]_secret|secret_[a-z0-9]"
)
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_SSN_LABELED = re.compile(  # unseparated or spaced SSNs are only recognizable by their label
    r"(?i)((?:\bssn|\bss#|\bsocial security(?:\s+(?:number|no\.?))?|\bi?tin)(?![a-z])[\s:#.]{0,4})"
    r"\d{3}[ -]?\d{2}[ -]?\d{4}(?!\d)"
)
# Card numbers as they are written: 4-4-4-4 or 4-6-5 groups with one consistent separator, or 13-19 digits.
_CARD = re.compile(r"(?<![\d-])(?:\d{4}([ -]?)\d{4}\1\d{4}\1\d{4}|\d{4}([ -]?)\d{6}\2\d{5}|\d{13,19})(?!\d)")
# US phones with punctuation (space-only groups are too often table columns), or labeled phones in any format.
_PHONE = re.compile(
    r"(?<![\w-])(?:(?:\+?1[ .-]?)?(?:\(\d{3}\)[ ]?\d{3}[ .-]\d{4}|\d{3}([.-])\d{3}\1\d{4})|\+1 \d{3} \d{3} \d{4})"
    r"(?![\w-])"
)
_PHONE_LABELED = re.compile(
    r"(?i)(\b(?:phone|tel|telephone|cell|mobile|fax)(?![a-z])[\s:#.]{0,4}(?:(?:number|no\.?)[\s:#.]{0,3})?)"
    r"\+?1?[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)"
)


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

    text = sub("secret", _PEM, "[SECRET]", text)
    text = sub("secret", _TOKEN, "[SECRET]", text)

    def field(match: re.Match[str]) -> str:
        if not _CREDENTIAL_NAME.search(match.group(1)):
            return match.group()
        counts["secret"] += 1
        return f"{match.group(1)}{match.group(2)}[SECRET]"

    text = _FIELD.sub(field, text)
    text = sub("email", _EMAIL, "[EMAIL]", text)
    text = sub("ssn", _SSN, "[SSN]", text)
    text = sub("ssn", _SSN_LABELED, r"\1[SSN]", text)

    def card(match: re.Match[str]) -> str:
        if _luhn(match.group()):
            counts["card"] += 1
            return "[CARD]"
        return match.group()

    text = _CARD.sub(card, text)
    text = sub("phone", _PHONE, "[PHONE]", text)
    text = sub("phone", _PHONE_LABELED, r"\1[PHONE]", text)
    return text, {kind: count for kind, count in counts.items() if count}
