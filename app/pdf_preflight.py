"""Cheap PDF sheet-type classification performed before Audiveris OMR.

The classifier deliberately prefers ``unknown`` over a confident wrong route.
It uses Poppler text extraction plus a low-resolution first-page raster, both of
which are substantially cheaper than running the full Audiveris pipeline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Literal


SheetType = Literal["chord-lyrics", "standard-notation", "unknown"]

CHORD_TOKEN = re.compile(
    r"(?<![A-Za-z])(?:[A-G](?:#|b)?(?:maj|min|m|dim|aug|sus|add)?\d*(?:/[A-G](?:#|b)?)?)(?![A-Za-z])",
)
INSTRUMENT_TERMS = {
    "ukulele": "ukulele",
    "uke": "ukulele",
    "guitar": "guitar",
    "banjo": "banjo",
    "mandolin": "mandolin",
}


@dataclass(frozen=True)
class InstrumentCandidate:
    instrument: str
    confidence: float
    evidence: list[str]


@dataclass(frozen=True)
class PreflightResult:
    sheet_type: SheetType
    confidence: float
    evidence: list[str]
    instrument_candidates: list[InstrumentCandidate]
    extracted_text: bool
    staff_systems: int

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["sheetType"] = result.pop("sheet_type")
        result["instrumentCandidates"] = result.pop("instrument_candidates")
        result["extractedText"] = result.pop("extracted_text")
        result["staffSystems"] = result.pop("staff_systems")
        return result


def classify_pdf(path: str | Path) -> PreflightResult:
    """Extract inexpensive evidence and classify a PDF before OMR."""
    pdf_path = Path(path)
    text = _extract_text(pdf_path)
    staff_systems = _detect_staff_systems(pdf_path)
    return classify_evidence(text=text, staff_systems=staff_systems)


def classify_evidence(*, text: str, staff_systems: int) -> PreflightResult:
    """Pure classification boundary used by tests and future extractors."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lowered = text.lower()
    evidence: list[str] = []
    chord_score = 0

    chord_tokens = CHORD_TOKEN.findall(text)
    chord_lines = sum(1 for line in lines if len(CHORD_TOKEN.findall(line)) >= 2)
    beat_rows = sum(
        1
        for line in lines
        if len(re.findall(r"(?<!\w)\.(?!\w)", line)) >= 3
        and bool(CHORD_TOKEN.search(line))
    )
    lyric_rows = sum(
        1
        for line in lines
        if len(re.findall(r"\b[A-Za-z']{2,}\b", line)) >= 4
        and len(CHORD_TOKEN.findall(line)) <= 1
    )

    if len(chord_tokens) >= 6 and chord_lines >= 1:
        chord_score += 3
        evidence.append("Repeated chord-symbol rows detected")
    if beat_rows:
        chord_score += 4
        evidence.append("Chord symbols and repeated beat dots share rows")
    if lyric_rows >= 2:
        chord_score += 2
        evidence.append("Lyric-like text rows detected")

    instruments: list[InstrumentCandidate] = []
    for term, instrument in INSTRUMENT_TERMS.items():
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            label = instrument.capitalize()
            evidence_text = f'Text explicitly mentions "{label}"'
            instruments.append(
                InstrumentCandidate(instrument, 0.97, [evidence_text])
            )
            chord_score += 2
            evidence.append(evidence_text)
            break

    if staff_systems == 0:
        evidence.append("No conventional five-line staff system detected")
    else:
        evidence.append(
            f"Detected {staff_systems} conventional five-line staff system(s)"
        )

    if chord_score >= 6 and staff_systems == 0:
        confidence = min(0.99, 0.72 + (chord_score - 6) * 0.045)
        return PreflightResult(
            "chord-lyrics",
            confidence,
            evidence,
            instruments,
            bool(text.strip()),
            staff_systems,
        )

    if staff_systems >= 1:
        confidence = min(0.98, 0.82 + 0.04 * min(staff_systems, 4))
        return PreflightResult(
            "standard-notation",
            confidence,
            evidence,
            instruments,
            bool(text.strip()),
            staff_systems,
        )

    return PreflightResult(
        "unknown",
        0.35 if text.strip() else 0.0,
        evidence or ["No decisive preflight evidence found"],
        instruments,
        bool(text.strip()),
        staff_systems,
    )


def _extract_text(path: Path) -> str:
    try:
        completed = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return completed.stdout.decode("utf-8", errors="replace")


def _detect_staff_systems(path: Path) -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="notestream-preflight-") as tmpdir:
            output_root = Path(tmpdir) / "first-page"
            subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    "1",
                    "-singlefile",
                    "-r",
                    "72",
                    "-gray",
                    str(path),
                    str(output_root),
                ],
                check=True,
                capture_output=True,
                timeout=20,
            )
            width, height, pixels = _read_pgm(
                output_root.with_suffix(".pgm").read_bytes()
            )
    except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError):
        return 0

    dark_rows = []
    for y in range(height):
        row = pixels[y * width : (y + 1) * width]
        # Low-resolution antialiasing turns thin black staff rules medium gray.
        if sum(value < 200 for value in row) >= width * 0.38:
            dark_rows.append(y)

    centers: list[float] = []
    for y in dark_rows:
        if not centers or y - centers[-1] > 2:
            centers.append(float(y))
        else:
            centers[-1] = (centers[-1] + y) / 2

    systems = 0
    index = 0
    while index + 4 < len(centers):
        group = centers[index : index + 5]
        gaps = [group[i + 1] - group[i] for i in range(4)]
        average = sum(gaps) / 4
        if 3 <= average <= 18 and all(abs(gap - average) <= 2 for gap in gaps):
            systems += 1
            index += 5
        else:
            index += 1
    return systems


def _read_pgm(data: bytes) -> tuple[int, int, bytes]:
    if not data.startswith(b"P5"):
        raise ValueError("Expected binary PGM data")
    tokens: list[bytes] = []
    position = 2
    while len(tokens) < 3:
        while position < len(data) and chr(data[position]).isspace():
            position += 1
        if position < len(data) and data[position] == ord("#"):
            position = data.find(b"\n", position) + 1
            continue
        end = position
        while end < len(data) and not chr(data[end]).isspace():
            end += 1
        tokens.append(data[position:end])
        position = end
    if position >= len(data) or not chr(data[position]).isspace():
        raise ValueError("PGM header is missing its pixel-data delimiter")
    position += 1
    width, height, maximum = map(int, tokens)
    if maximum != 255 or len(data) - position < width * height:
        raise ValueError("Unsupported or incomplete PGM data")
    return width, height, data[position : position + width * height]
