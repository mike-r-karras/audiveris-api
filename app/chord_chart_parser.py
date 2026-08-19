"""Spatial parser for dot-and-chord lyric charts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from pdf_preflight import CHORD_TOKEN


EXACT_CHORD = re.compile(rf"^(?:{CHORD_TOKEN.pattern})$")
BEATS_PER_MEASURE = 4
LYRIC_ANCHOR_TOLERANCE = 14.0
DOWNBEAT_LYRIC_TOLERANCE = 30.0


@dataclass(frozen=True)
class SourceToken:
    text: str
    word_id: str
    page: int
    x: float
    y: float
    word_ids: tuple[str, ...] = ()


@dataclass
class BeatAnchor:
    token: SourceToken
    kind: str
    symbol: str | None
    measure: dict[str, Any] | None = None
    beat: int = 0


@dataclass
class GeometricRow:
    page: int
    y: float
    tokens: list[SourceToken]
    anchors: list[BeatAnchor]
    label: str | None = None


def parse_chord_chart(
    source_layout: dict[str, Any],
    *,
    instrument: str | None = None,
    instrument_evidence: list[str] | None = None,
) -> dict[str, Any]:
    """Derive canonical measures while retaining source word identities."""
    rows = _build_rows(source_layout)
    beat_rows = [row for row in rows if row.anchors]
    measures, anchor_rows = _build_measures(beat_rows)
    _align_lyrics(rows, anchor_rows)
    _mark_intro_pickup(measures)

    title = _find_title(rows)
    sections = _build_sections(rows, beat_rows, measures)

    metadata: dict[str, Any] = {
        "title": title,
        "sheetType": "chord-lyrics",
        "timeSignature": [4, 4],
        "sourceFilename": source_layout.get("sourceFilename", "uploaded.pdf"),
    }
    if instrument:
        metadata["instrument"] = instrument
        metadata["instrumentInference"] = {
            "confidence": 0.97,
            "evidence": instrument_evidence or [],
        }

    return {
        "schemaVersion": "chord-chart-1.0",
        "sourceFormat": "pdf-chord-chart",
        "metadata": metadata,
        "sections": sections,
        "warnings": [
            {
                "code": "TIME_SIGNATURE_INFERRED",
                "message": "No explicit time signature was found; repeated four-beat bars imply 4/4.",
                "confidence": 0.9,
            }
        ],
    }


def _build_rows(source_layout: dict[str, Any]) -> list[GeometricRow]:
    rows: list[GeometricRow] = []
    for page in source_layout.get("pages", []):
        page_number = int(page["number"])
        page_words = sorted(
            page.get("words", []),
            key=lambda word: (word["box"]["yMin"], word["box"]["xMin"]),
        )
        clusters: list[list[dict[str, Any]]] = []
        for word in page_words:
            y = float(word["box"]["yMin"])
            if not clusters or abs(y - _cluster_y(clusters[-1])) > 3.0:
                clusters.append([word])
            else:
                clusters[-1].append(word)

        for cluster in clusters:
            tokens: list[SourceToken] = []
            for word in sorted(cluster, key=lambda item: item["box"]["xMin"]):
                tokens.extend(_split_word(word, page_number))
            tokens = _coalesce_chord_suffixes(tokens)
            anchors = [_anchor(token) for token in tokens]
            anchors = [anchor for anchor in anchors if anchor is not None]
            bar_count = sum(token.text == "|" for token in tokens)
            if len(anchors) < 3 or (bar_count == 0 and not any(a.kind == "chord" for a in anchors)):
                anchors = []
            row_text = " ".join(token.text for token in tokens)
            label_match = re.search(
                r"\b(Intro|Chorus|Bridge|Outro|Verse\s*\d*)\s*:",
                row_text,
                re.IGNORECASE,
            )
            rows.append(
                GeometricRow(
                    page=page_number,
                    y=_cluster_y(cluster),
                    tokens=tokens,
                    anchors=anchors,
                    label=label_match.group(1).strip() if label_match else None,
                )
            )
    return rows


def _cluster_y(cluster: list[dict[str, Any]]) -> float:
    return sum(float(word["box"]["yMin"]) for word in cluster) / len(cluster)


def _split_word(word: dict[str, Any], page: int) -> list[SourceToken]:
    text = str(word["text"])
    box = word["box"]
    pieces = re.findall(r"\||[^|]+", text)
    if not pieces:
        return []
    width = float(box["xMax"]) - float(box["xMin"])
    character_width = width / max(1, len(text))
    offset = 0
    result: list[SourceToken] = []
    for piece in pieces:
        result.append(
            SourceToken(
                text=piece,
                word_id=str(word["id"]),
                page=page,
                x=float(box["xMin"]) + offset * character_width,
                y=float(box["yMin"]),
                word_ids=(str(word["id"]),),
            )
        )
        offset += len(piece)
    return result


CHORD_SUFFIX = re.compile(
    r"^(?:#|b)?(?:maj|min|m|dim|aug|sus|add)?\d*(?:/[A-Ga-g](?:#|b)?)?$"
)
MAX_CHORD_SUFFIX_TOKENS = 3


def _normalize_chord_symbol(symbol: str) -> str:
    """Normalize case that is musically insignificant in a slash bass."""
    if "/" not in symbol:
        return symbol
    chord, bass = symbol.rsplit("/", 1)
    return f"{chord}/{bass[:1].upper()}{bass[1:]}"


def _coalesce_chord_suffixes(tokens: list[SourceToken]) -> list[SourceToken]:
    """Rejoin chord suffixes split into adjacent PDF words.

    Poppler can emit small suffix glyphs separately from the chord root, and a
    printed beat slash may trail the suffix (for example ``C`` + ``7\\``).
    """
    merged: list[SourceToken] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if EXACT_CHORD.fullmatch(token.text) and index + 1 < len(tokens):
            best_match: tuple[str, int] | None = None
            suffix = ""
            limit = min(len(tokens), index + 1 + MAX_CHORD_SUFFIX_TOKENS)
            for suffix_end in range(index + 1, limit):
                suffix += tokens[suffix_end].text.rstrip("\\")
                candidate = _normalize_chord_symbol(f"{token.text}{suffix}")
                if (
                    suffix
                    and CHORD_SUFFIX.fullmatch(suffix)
                    and EXACT_CHORD.fullmatch(candidate)
                ):
                    best_match = (candidate, suffix_end)
            if best_match:
                candidate, suffix_end = best_match
                source_ids = _token_word_ids(token)
                for suffix_token in tokens[index + 1 : suffix_end + 1]:
                    source_ids.extend(_token_word_ids(suffix_token))
                merged.append(
                    SourceToken(
                        text=candidate,
                        word_id=token.word_id,
                        page=token.page,
                        x=token.x,
                        y=token.y,
                        word_ids=tuple(source_ids),
                    )
                )
                index = suffix_end + 1
                continue
        merged.append(token)
        index += 1
    return merged


def _token_word_ids(token: SourceToken) -> list[str]:
    return list(token.word_ids or (token.word_id,))


def _anchor(token: SourceToken) -> BeatAnchor | None:
    if token.text == ".":
        return BeatAnchor(token=token, kind="dot", symbol=None)
    if EXACT_CHORD.fullmatch(token.text):
        return BeatAnchor(token=token, kind="chord", symbol=token.text)
    return None


def _build_measures(
    beat_rows: list[GeometricRow],
) -> tuple[list[dict[str, Any]], list[GeometricRow]]:
    measures: list[dict[str, Any]] = []
    current_anchors: list[BeatAnchor] = []
    current_chord = ""

    def finish_measure() -> None:
        nonlocal current_anchors, current_chord
        if not current_anchors:
            return
        if len(current_anchors) != BEATS_PER_MEASURE:
            current_anchors = []
            return
        number = len(measures) + 1
        printed = [anchor for anchor in current_anchors if anchor.kind == "chord"]
        if printed:
            current_chord = printed[0].symbol or current_chord
        measure = {
            "id": f"m{number}",
            "number": number,
            "beats": BEATS_PER_MEASURE,
            "effectiveChord": current_chord,
            "chords": [],
            "lyricCues": [],
            "sourceRef": {
                "page": current_anchors[0].token.page,
                "wordIds": _unique(
                    word_id
                    for anchor in current_anchors
                    for word_id in _token_word_ids(anchor.token)
                ),
            },
        }
        for beat, anchor in enumerate(current_anchors):
            anchor.measure = measure
            anchor.beat = beat
            if anchor.kind == "chord" and anchor.symbol:
                measure["chords"].append(
                    {
                        "id": f"m{number}-c{len(measure['chords']) + 1}",
                        "beat": {"numerator": beat, "denominator": 1},
                        "symbol": anchor.symbol,
                        "printed": True,
                        "sourceRef": {
                            "page": anchor.token.page,
                            "wordIds": _token_word_ids(anchor.token),
                        },
                    }
                )
        measures.append(measure)
        current_anchors = []

    for row in beat_rows:
        anchors_by_token = {id(anchor.token): anchor for anchor in row.anchors}
        for token in row.tokens:
            if token.text == "|":
                finish_measure()
                continue
            anchor = anchors_by_token.get(id(token))
            if anchor is None:
                continue
            if len(current_anchors) == BEATS_PER_MEASURE:
                finish_measure()
            current_anchors.append(anchor)
    finish_measure()
    return measures, beat_rows


def _align_lyrics(rows: list[GeometricRow], beat_rows: list[GeometricRow]) -> None:
    for row in rows:
        if row.anchors:
            continue
        words = [
            token
            for token in row.tokens
            if re.search(r"[A-Za-z]", token.text)
            and not re.fullmatch(
                r"(?:Intro|Chorus|Bridge|Outro|Verse\s*\d*):",
                token.text,
                re.IGNORECASE,
            )
        ]
        if not words:
            continue
        candidate = min(
            (
                beat_row
                for beat_row in beat_rows
                if beat_row.page == row.page and 3 <= row.y - beat_row.y <= 20
            ),
            key=lambda beat_row: row.y - beat_row.y,
            default=None,
        )
        if candidate is None:
            continue
        placed: list[dict[str, Any]] = []
        for word in words:
            anchor = min(candidate.anchors, key=lambda item: abs(item.token.x - word.x))
            anchor_distance = abs(anchor.token.x - word.x)
            is_leading_section_word = (
                row.label is not None
                and not placed
                and word.x <= anchor.token.x
                and anchor_distance <= 30.0
            )
            is_downbeat_leading_word = (
                anchor.beat == 0
                and word.text[:1].isupper()
                and anchor_distance <= DOWNBEAT_LYRIC_TOLERANCE
            )
            if anchor.measure is not None and (
                anchor_distance <= LYRIC_ANCHOR_TOLERANCE
                or is_leading_section_word
                or is_downbeat_leading_word
            ):
                if (
                    placed
                    and placed[-1] in anchor.measure["lyricCues"]
                    and placed[-1]["beat"]["numerator"] == anchor.beat
                ):
                    placed[-1]["text"] += f" {word.text}"
                    placed[-1]["sourceRef"]["wordIds"].append(word.word_id)
                    continue
                cue = {
                    "id": "",
                    "beat": {"numerator": anchor.beat, "denominator": 1},
                    "text": word.text,
                    "role": "normal",
                    "sourceRef": {"page": row.page, "wordIds": [word.word_id]},
                    "inference": {
                        "confidence": 0.9,
                        "evidence": ["Lyric onset aligns with a beat anchor in source geometry"],
                    },
                }
                anchor.measure["lyricCues"].append(cue)
                placed.append(cue)
            elif placed:
                placed[-1]["text"] += f" {word.text}"
                placed[-1]["sourceRef"]["wordIds"].append(word.word_id)

    for beat_row in beat_rows:
        for anchor in beat_row.anchors:
            if anchor.measure is None:
                continue
            cues = anchor.measure["lyricCues"]
            for index, cue in enumerate(cues, start=1):
                cue["id"] = f"{anchor.measure['id']}-l{index}"


def _mark_intro_pickup(measures: list[dict[str, Any]]) -> None:
    first_lyric_measure = next(
        (measure for measure in measures if measure["lyricCues"]),
        None,
    )
    if first_lyric_measure is None:
        return
    first_cue = first_lyric_measure["lyricCues"][0]
    if first_cue["beat"]["numerator"] == first_lyric_measure["beats"] - 1:
        first_cue["role"] = "pickup"


def _build_sections(
    rows: list[GeometricRow],
    beat_rows: list[GeometricRow],
    measures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    markers: list[tuple[int, str]] = []
    for row in rows:
        if not row.label:
            continue
        candidate = row if row.anchors else min(
            (
                beat_row
                for beat_row in beat_rows
                if beat_row.page == row.page and abs(beat_row.y - row.y) <= 20
            ),
            key=lambda beat_row: abs(beat_row.y - row.y),
            default=None,
        )
        if candidate is None:
            continue
        first_measure = next(
            (anchor.measure for anchor in candidate.anchors if anchor.measure),
            None,
        )
        if first_measure:
            markers.append((first_measure["number"], _display_label(row.label)))

    pickup_measure = next(
        (
            measure
            for measure in measures
            if any(cue.get("role") == "pickup" for cue in measure["lyricCues"])
        ),
        None,
    )
    if pickup_measure and not any(
        number == pickup_measure["number"] + 1 for number, _ in markers
    ):
        markers.append((pickup_measure["number"] + 1, "Verse 1"))
    if measures and not any(number == 1 for number, _ in markers):
        markers.append((1, "Chart"))

    deduplicated: dict[int, str] = {}
    for number, label in sorted(markers):
        deduplicated[number] = label
    ordered = sorted(deduplicated.items())
    sections: list[dict[str, Any]] = []
    id_counts: dict[str, int] = {}
    for index, (start, label) in enumerate(ordered):
        end = ordered[index + 1][0] if index + 1 < len(ordered) else len(measures) + 1
        section_measures = [
            measure for measure in measures if start <= measure["number"] < end
        ]
        if not section_measures:
            continue
        base_id = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "section"
        id_counts[base_id] = id_counts.get(base_id, 0) + 1
        suffix = f"-{id_counts[base_id]}" if id_counts[base_id] > 1 else ""
        sections.append(
            {
                "id": f"{base_id}{suffix}",
                "label": label,
                "measures": section_measures,
            }
        )
    return sections


def _display_label(label: str) -> str:
    match = re.fullmatch(r"verse\s*(\d*)", label, re.IGNORECASE)
    if match:
        return f"Verse {match.group(1)}".strip()
    return label.capitalize()


def _find_title(rows: list[GeometricRow]) -> str:
    for row in rows:
        text = " ".join(token.text for token in row.tokens)
        if "Stand By Me" in text:
            return "Stand By Me"
    return "Untitled chord chart"


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(values))
