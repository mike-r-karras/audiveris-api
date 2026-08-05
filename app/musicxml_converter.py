from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias


PitchStep: TypeAlias = Literal["A", "B", "C", "D", "E", "F", "G"]
ClefSign: TypeAlias = Literal["G", "F", "C", "percussion", "TAB"]
VexDuration: TypeAlias = Literal[
    "w",
    "h",
    "q",
    "8",
    "16",
    "32",
    "64",
    "128",
]


class MusicXMLConversionError(ValueError):
    """Raised when a MusicXML document cannot be converted."""


@dataclass
class ConversionWarning:
    code: str
    message: str
    part_id: str | None = None
    measure_number: int | None = None


@dataclass
class ScoreMetadata:
    title: str | None = None
    movement_title: str | None = None
    composer: str | None = None


@dataclass
class TimeSignature:
    beats: int
    beat_type: int


@dataclass
class KeySignature:
    fifths: int
    mode: str | None = None


@dataclass
class Clef:
    sign: ClefSign
    line: int | None = None


@dataclass
class MeasureAttributes:
    divisions: int = 1
    time: TimeSignature | None = None
    key: KeySignature | None = None

    # Backward-compatible staff-1 clef for existing consumers.
    clef: Clef | None = None

    # Complete staff-specific clef map.
    clefs: dict[int, Clef] = field(default_factory=dict)


@dataclass
class Pitch:
    step: PitchStep
    octave: int

    # -2 = double flat
    # -1 = flat
    #  0 = natural
    #  1 = sharp
    #  2 = double sharp
    alter: float = 0


@dataclass
class EventDuration:
    # Raw MusicXML duration in division units.
    divisions: int

    # Duration measured in quarter notes.
    quarter_notes: float

    # Best matching VexFlow duration.
    vexflow: VexDuration

    dots: int = 0


@dataclass
class NormalizationRecord:
    type: str
    confidence: float
    reason: str
    original_duration: EventDuration


@dataclass
class BaseEvent:
    id: str
    type: str
    measure_number: int
    voice: int
    staff: int
    start_divisions: int
    start_quarter_notes: float
    duration: EventDuration
    normalization: NormalizationRecord | None = None


@dataclass
class NoteEvent(BaseEvent):
    pitches: list[Pitch] = field(default_factory=list)
    accidentals: list[str | None] | None = None


@dataclass
class RestEvent(BaseEvent):
    pass


ScoreEvent: TypeAlias = NoteEvent | RestEvent


@dataclass
class Voice:
    id: str
    number: int
    staff: int = 1
    events: list[ScoreEvent] = field(default_factory=list)


@dataclass
class Measure:
    id: str
    number: int
    attributes: MeasureAttributes
    voices: list[Voice] = field(default_factory=list)


@dataclass
class Part:
    id: str
    measures: list[Measure]
    name: str | None = None


@dataclass
class NotestreamScore:
    schema_version: str
    source_format: str
    metadata: ScoreMetadata
    parts: list[Part]
    warnings: list[ConversionWarning]


def convert_musicxml(xml: str | bytes) -> dict[str, Any]:
    """
    Convert score-partwise MusicXML into Notestream JSON-compatible data.

    Current scope:
    - first part only
    - multiple staves and voices
    - backup / forward timing
    - notes, rests and chords
    - divisions
    - time signatures
    - key signatures
    - staff-specific clefs
    - dots
    - written accidentals

    Deferred:
    - ties
    - beams
    - tuplets
    - lyrics
    - grace notes
    - repeats
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise MusicXMLConversionError(
            f"Invalid MusicXML: {exc}"
        ) from exc

    strip_namespaces(root)

    if root.tag == "score-timewise":
        raise MusicXMLConversionError(
            "score-timewise MusicXML is not supported in converter version 1."
        )

    if root.tag != "score-partwise":
        raise MusicXMLConversionError(
            "The document does not contain a score-partwise root element."
        )

    warnings: list[ConversionWarning] = []
    parts_xml = root.findall("part")

    if not parts_xml:
        raise MusicXMLConversionError(
            "The MusicXML document does not contain any parts."
        )

    if len(parts_xml) > 1:
        warnings.append(
            ConversionWarning(
                code="MULTIPLE_PARTS_IGNORED",
                message=(
                    f"The score contains {len(parts_xml)} parts. "
                    "Converter version 1 only converts the first part."
                ),
            )
        )

    part_names = read_part_names(root)
    first_part_xml = parts_xml[0]
    part_id = first_part_xml.get("id") or "P1"

    part = convert_part(
        part_xml=first_part_xml,
        part_id=part_id,
        part_name=part_names.get(part_id),
        warnings=warnings,
    )

    score = NotestreamScore(
        schema_version="1.0",
        source_format="musicxml",
        metadata=read_metadata(root),
        parts=[part],
        warnings=warnings,
    )

    return dataclass_to_camel_case_dict(score)


def convert_musicxml_file(
    input_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Convert a MusicXML file.

    If output_path is supplied, write formatted JSON to that path.
    """
    input_file = Path(input_path)

    if not input_file.exists():
        raise FileNotFoundError(
            f"MusicXML file does not exist: {input_file}"
        )

    import musicxml_decompressor
    xml = musicxml_decompressor.load_musicxml(input_file)
    result = convert_musicxml(xml)

    if output_path is not None:
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return result


def convert_part(
    part_xml: ET.Element,
    part_id: str,
    part_name: str | None,
    warnings: list[ConversionWarning],
) -> Part:
    measures_xml = part_xml.findall("measure")

    current_attributes = MeasureAttributes(divisions=1)
    measures: list[Measure] = []

    for index, measure_xml in enumerate(measures_xml):
        measure_number = parse_int(measure_xml.get("number"))

        if measure_number is None:
            measure_number = index + 1

        measure, current_attributes = convert_measure(
            measure_xml=measure_xml,
            part_id=part_id,
            measure_number=measure_number,
            previous_attributes=current_attributes,
            warnings=warnings,
        )

        measures.append(measure)

    return Part(
        id=part_id,
        name=part_name,
        measures=measures,
    )


def convert_measure(
    measure_xml: ET.Element,
    part_id: str,
    measure_number: int,
    previous_attributes: MeasureAttributes,
    warnings: list[ConversionWarning],
) -> tuple[Measure, MeasureAttributes]:
    attributes = read_measure_attributes(
        measure_xml.find("attributes"),
        previous_attributes,
    )

    cursor_divisions = 0
    event_sequence = 0
    events_by_voice: dict[tuple[int, int], list[ScoreEvent]] = {}
    last_note_by_voice: dict[tuple[int, int], NoteEvent] = {}
    normalization_blocked_event_ids: set[str] = set()

    for child in list(measure_xml):
        if child.tag == "backup":
            duration = child_int(child, "duration")
            if duration is None or duration < 0:
                warnings.append(
                    ConversionWarning(
                        code="INVALID_BACKUP_DURATION",
                        message="A backup element had a missing or invalid duration.",
                        part_id=part_id,
                        measure_number=measure_number,
                    )
                )
                continue

            cursor_divisions -= duration
            if cursor_divisions < 0:
                warnings.append(
                    ConversionWarning(
                        code="BACKUP_BEFORE_MEASURE_START",
                        message=(
                            "A backup moved the MusicXML cursor before the "
                            "start of the measure; the cursor was clamped to zero."
                        ),
                        part_id=part_id,
                        measure_number=measure_number,
                    )
                )
                cursor_divisions = 0
            continue

        if child.tag == "forward":
            duration = child_int(child, "duration")
            if duration is None or duration < 0:
                warnings.append(
                    ConversionWarning(
                        code="INVALID_FORWARD_DURATION",
                        message="A forward element had a missing or invalid duration.",
                        part_id=part_id,
                        measure_number=measure_number,
                    )
                )
                continue
            cursor_divisions += duration
            continue

        if child.tag != "note":
            continue

        note_xml = child
        voice_number = child_int(note_xml, "voice") or 1
        staff_number = child_int(note_xml, "staff") or 1
        voice_key = (staff_number, voice_number)

        if note_xml.find("grace") is not None:
            warnings.append(
                ConversionWarning(
                    code="GRACE_NOTE_IGNORED",
                    message="A grace note was ignored.",
                    part_id=part_id,
                    measure_number=measure_number,
                )
            )
            continue

        raw_duration = child_int(note_xml, "duration")
        if raw_duration is None or raw_duration < 0:
            warnings.append(
                ConversionWarning(
                    code="INVALID_NOTE_DURATION",
                    message="A note with a missing or invalid duration was ignored.",
                    part_id=part_id,
                    measure_number=measure_number,
                )
            )
            continue

        duration = create_duration(
            raw_duration=raw_duration,
            divisions=attributes.divisions,
            dots=len(note_xml.findall("dot")),
            musicxml_type=child_text(note_xml, "type"),
        )

        is_chord_tone = note_xml.find("chord") is not None
        is_rest = note_xml.find("rest") is not None

        if is_chord_tone:
            last_note_event = last_note_by_voice.get(voice_key)
            if last_note_event is None:
                warnings.append(
                    ConversionWarning(
                        code="ORPHAN_CHORD_TONE",
                        message=(
                            "A note contained <chord/> but there was no preceding "
                            "note in the same staff and voice to attach it to."
                        ),
                        part_id=part_id,
                        measure_number=measure_number,
                    )
                )
                continue

            pitch = read_pitch(note_xml.find("pitch"))
            if pitch is None:
                warnings.append(
                    ConversionWarning(
                        code="CHORD_PITCH_MISSING",
                        message="A chord tone without a valid pitch was ignored.",
                        part_id=part_id,
                        measure_number=measure_number,
                    )
                )
                continue

            accidental = child_text(note_xml, "accidental")
            last_note_event.pitches.append(pitch)
            if last_note_event.accidentals is not None:
                last_note_event.accidentals.append(accidental)
            elif accidental is not None:
                last_note_event.accidentals = [
                    None for _ in range(len(last_note_event.pitches) - 1)
                ] + [accidental]
            continue

        event_sequence += 1
        event_id = (
            f"{part_id}-m{measure_number}-s{staff_number}"
            f"-v{voice_number}-e{event_sequence}"
        )
        start_divisions = cursor_divisions
        start_quarter_notes = cursor_divisions / attributes.divisions

        if is_rest:
            event: ScoreEvent = RestEvent(
                id=event_id,
                type="rest",
                measure_number=measure_number,
                voice=voice_number,
                staff=staff_number,
                start_divisions=start_divisions,
                start_quarter_notes=start_quarter_notes,
                duration=duration,
            )
            last_note_by_voice.pop(voice_key, None)
        else:
            pitch = read_pitch(note_xml.find("pitch"))
            if pitch is None:
                warnings.append(
                    ConversionWarning(
                        code="PITCH_MISSING",
                        message=(
                            "A pitched note without a valid pitch was ignored; "
                            "its duration was still applied to the cursor."
                        ),
                        part_id=part_id,
                        measure_number=measure_number,
                    )
                )
                cursor_divisions += raw_duration
                last_note_by_voice.pop(voice_key, None)
                continue

            accidental = child_text(note_xml, "accidental")
            event = NoteEvent(
                id=event_id,
                type="note",
                measure_number=measure_number,
                voice=voice_number,
                staff=staff_number,
                start_divisions=start_divisions,
                start_quarter_notes=start_quarter_notes,
                duration=duration,
                pitches=[pitch],
                accidentals=[accidental] if accidental is not None else None,
            )
            last_note_by_voice[voice_key] = event

        if (
            note_xml.find("tie") is not None
            or note_xml.find("notations/tied") is not None
            or note_xml.find("time-modification") is not None
        ):
            normalization_blocked_event_ids.add(event.id)

        events_by_voice.setdefault(voice_key, []).append(event)
        cursor_divisions += raw_duration

    voices = [
        Voice(
            id=f"{part_id}-m{measure_number}-s{staff}-v{voice}",
            number=voice,
            staff=staff,
            events=sorted(
                events,
                key=lambda event: (event.start_divisions, event.id),
            ),
        )
        for (staff, voice), events in sorted(events_by_voice.items())
    ]

    normalize_measure_duration(
        attributes=attributes,
        voices=voices,
        part_id=part_id,
        measure_number=measure_number,
        warnings=warnings,
        blocked_event_ids=normalization_blocked_event_ids,
    )

    validate_measure_duration(
        attributes=attributes,
        voices=voices,
        part_id=part_id,
        measure_number=measure_number,
        warnings=warnings,
    )

    measure = Measure(
        id=f"{part_id}-m{measure_number}",
        number=measure_number,
        attributes=attributes,
        voices=voices,
    )

    return measure, deepcopy(attributes)


def normalize_measure_duration(
    *,
    attributes: MeasureAttributes,
    voices: list[Voice],
    part_id: str,
    measure_number: int,
    warnings: list[ConversionWarning],
    blocked_event_ids: set[str],
) -> None:
    """Conservatively trim a single terminal event that crosses the barline.

    The repair is intentionally narrow: it never moves an onset, never changes
    an interior event, and skips tied or tuplet-based source events. The
    original duration is retained on the event in normalization metadata.
    """
    if attributes.time is None or attributes.divisions <= 0:
        return

    expected_quarter_notes = attributes.time.beats * 4 / attributes.time.beat_type
    expected_divisions_float = expected_quarter_notes * attributes.divisions
    expected_divisions = round(expected_divisions_float)

    # MusicXML duration values are integral. Do not normalize when the declared
    # bar length cannot be represented exactly with the current divisions.
    if not math.isclose(expected_divisions_float, expected_divisions, abs_tol=1e-9):
        return

    for voice in voices:
        if not voice.events:
            continue

        crossing_events = [
            event
            for event in voice.events
            if event.start_divisions < expected_divisions
            and event.start_divisions + event.duration.divisions > expected_divisions
        ]
        if len(crossing_events) != 1:
            continue

        event = crossing_events[0]
        if event.id in blocked_event_ids:
            continue

        # Only repair the chronologically final event in the voice.
        final_event = max(
            voice.events,
            key=lambda candidate: (
                candidate.start_divisions + candidate.duration.divisions,
                candidate.start_divisions,
                candidate.id,
            ),
        )
        if final_event.id != event.id:
            continue

        normalized_divisions = expected_divisions - event.start_divisions
        if normalized_divisions <= 0 or normalized_divisions >= event.duration.divisions:
            continue

        normalized_quarter_notes = normalized_divisions / attributes.divisions
        normalized_vexflow = nearest_vex_duration(normalized_quarter_notes, 0)
        vex_quarter_notes = vex_duration_quarter_notes(normalized_vexflow)

        # Pitched events are changed only when the replacement is an exact,
        # conventional undotted duration. Terminal rests may be trimmed to the
        # exact remaining bar duration because they are spacing events.
        if isinstance(event, NoteEvent) and not math.isclose(
            normalized_quarter_notes,
            vex_quarter_notes,
            abs_tol=1e-9,
        ):
            continue

        original_duration = deepcopy(event.duration)
        event.duration = EventDuration(
            divisions=normalized_divisions,
            quarter_notes=normalized_quarter_notes,
            vexflow=normalized_vexflow,
            dots=0,
        )
        confidence = 0.96 if isinstance(event, NoteEvent) else 0.99
        event.normalization = NormalizationRecord(
            type="trim-at-measure-boundary",
            confidence=confidence,
            reason=(
                "Terminal event crossed the declared measure boundary; its "
                "onset was preserved and its duration was shortened to end "
                "exactly at the barline."
            ),
            original_duration=original_duration,
        )
        warnings.append(
            ConversionWarning(
                code="MEASURE_AUTO_NORMALIZED",
                message=(
                    f"Staff {voice.staff}, voice {voice.number}: event {event.id} "
                    f"was shortened from {original_duration.quarter_notes:g} to "
                    f"{normalized_quarter_notes:g} quarter notes so the measure "
                    "ends at the declared barline."
                ),
                part_id=part_id,
                measure_number=measure_number,
            )
        )


def vex_duration_quarter_notes(duration: VexDuration) -> float:
    return {
        "w": 4.0,
        "h": 2.0,
        "q": 1.0,
        "8": 0.5,
        "16": 0.25,
        "32": 0.125,
        "64": 0.0625,
        "128": 0.03125,
    }[duration]


def validate_measure_duration(
    *,
    attributes: MeasureAttributes,
    voices: list[Voice],
    part_id: str,
    measure_number: int,
    warnings: list[ConversionWarning],
) -> None:
    """Warn when any staff/voice extends beyond the declared time signature."""
    if attributes.time is None or attributes.divisions <= 0:
        return

    expected_quarter_notes = (
        attributes.time.beats * 4 / attributes.time.beat_type
    )
    expected_divisions = expected_quarter_notes * attributes.divisions

    for voice in voices:
        if not voice.events:
            continue

        actual_divisions = max(
            event.start_divisions + event.duration.divisions
            for event in voice.events
        )

        if actual_divisions > expected_divisions + 1e-9:
            actual_quarter_notes = actual_divisions / attributes.divisions
            warnings.append(
                ConversionWarning(
                    code="MEASURE_OVERFULL",
                    message=(
                        f"Staff {voice.staff}, voice {voice.number} extends to "
                        f"{actual_quarter_notes:g} quarter notes, but the "
                        f"declared measure duration is "
                        f"{expected_quarter_notes:g} quarter notes."
                    ),
                    part_id=part_id,
                    measure_number=measure_number,
                )
            )


def read_measure_attributes(
    attributes_xml: ET.Element | None,
    previous: MeasureAttributes,
) -> MeasureAttributes:
    if attributes_xml is None:
        return deepcopy(previous)

    divisions = (
        child_positive_int(attributes_xml, "divisions")
        or previous.divisions
    )

    result = deepcopy(previous)
    result.divisions = divisions

    key_xml = attributes_xml.find("key")
    time_xml = attributes_xml.find("time")

    if key_xml is not None:
        result.key = KeySignature(
            fifths=child_int(key_xml, "fifths") or 0,
            mode=child_text(key_xml, "mode"),
        )

    if time_xml is not None:
        time_signature = read_time_signature(time_xml)
        if time_signature is not None:
            result.time = time_signature

    for clef_xml in attributes_xml.findall("clef"):
        clef = read_clef(clef_xml)
        if clef is None:
            continue

        staff_number = parse_int(clef_xml.get("number")) or 1
        result.clefs[staff_number] = clef

    if 1 in result.clefs:
        result.clef = deepcopy(result.clefs[1])
    elif result.clefs and result.clef is None:
        result.clef = deepcopy(result.clefs[min(result.clefs)])

    return result


def read_time_signature(
    time_xml: ET.Element,
) -> TimeSignature | None:
    beats = child_positive_int(time_xml, "beats")
    beat_type = child_positive_int(time_xml, "beat-type")

    if beats is None or beat_type is None:
        return None

    return TimeSignature(
        beats=beats,
        beat_type=beat_type,
    )


def read_clef(clef_xml: ET.Element) -> Clef | None:
    sign = child_text(clef_xml, "sign")

    valid_signs = {"G", "F", "C", "percussion", "TAB"}

    if sign not in valid_signs:
        return None

    return Clef(
        sign=sign,  # type: ignore[arg-type]
        line=child_positive_int(clef_xml, "line"),
    )


def read_pitch(pitch_xml: ET.Element | None) -> Pitch | None:
    if pitch_xml is None:
        return None

    step = child_text(pitch_xml, "step")
    octave = child_int(pitch_xml, "octave")
    alter = child_float(pitch_xml, "alter")

    if step is None:
        return None

    step = step.upper()

    if step not in {"A", "B", "C", "D", "E", "F", "G"}:
        return None

    if octave is None:
        return None

    return Pitch(
        step=step,  # type: ignore[arg-type]
        octave=octave,
        alter=alter if alter is not None else 0,
    )


def create_duration(
    raw_duration: int,
    divisions: int,
    dots: int,
    musicxml_type: str | None,
) -> EventDuration:
    if divisions <= 0:
        divisions = 1

    quarter_notes = raw_duration / divisions

    vexflow = (
        musicxml_type_to_vex_duration(musicxml_type)
        or nearest_vex_duration(quarter_notes, dots)
    )

    return EventDuration(
        divisions=raw_duration,
        quarter_notes=quarter_notes,
        vexflow=vexflow,
        dots=dots,
    )


def musicxml_type_to_vex_duration(
    musicxml_type: str | None,
) -> VexDuration | None:
    values: dict[str, VexDuration] = {
        "whole": "w",
        "half": "h",
        "quarter": "q",
        "eighth": "8",
        "16th": "16",
        "32nd": "32",
        "64th": "64",
        "128th": "128",
    }

    if musicxml_type is None:
        return None

    return values.get(musicxml_type)


def nearest_vex_duration(
    quarter_notes_including_dots: float,
    dots: int,
) -> VexDuration:
    dot_multiplier = calculate_dot_multiplier(dots)

    base_quarter_notes = (
        quarter_notes_including_dots / dot_multiplier
    )

    candidates: list[tuple[VexDuration, float]] = [
        ("w", 4),
        ("h", 2),
        ("q", 1),
        ("8", 0.5),
        ("16", 0.25),
        ("32", 0.125),
        ("64", 0.0625),
        ("128", 0.03125),
    ]

    return min(
        candidates,
        key=lambda candidate: abs(
            base_quarter_notes - candidate[1]
        ),
    )[0]


def calculate_dot_multiplier(dots: int) -> float:
    multiplier = 1.0
    addition = 0.5

    for _ in range(max(0, dots)):
        multiplier += addition
        addition /= 2

    return multiplier


def read_metadata(root: ET.Element) -> ScoreMetadata:
    work_xml = root.find("work")
    identification_xml = root.find("identification")

    title = (
        child_text(work_xml, "work-title")
        if work_xml is not None
        else None
    )

    movement_title = child_text(root, "movement-title")
    composer: str | None = None

    if identification_xml is not None:
        for creator in identification_xml.findall("creator"):
            creator_type = (
                creator.get("type") or ""
            ).strip().lower()

            if creator_type == "composer":
                composer = element_text(creator)
                break

    return ScoreMetadata(
        title=title,
        movement_title=movement_title,
        composer=composer,
    )


def read_part_names(root: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    part_list = root.find("part-list")

    if part_list is None:
        return result

    for score_part in part_list.findall("score-part"):
        part_id = score_part.get("id")
        part_name = child_text(score_part, "part-name")

        if part_id and part_name:
            result[part_id] = part_name

    return result


def strip_namespaces(element: ET.Element) -> None:
    """
    Remove XML namespaces in-place.

    MusicXML documents sometimes use a default namespace, which otherwise
    makes ElementTree paths look like '{namespace}part'.
    """
    for node in element.iter():
        if "}" in node.tag:
            node.tag = node.tag.split("}", 1)[1]


def element_text(element: ET.Element | None) -> str | None:
    if element is None:
        return None

    text = "".join(element.itertext()).strip()
    return text or None


def child_text(
    element: ET.Element | None,
    child_name: str,
) -> str | None:
    if element is None:
        return None

    return element_text(element.find(child_name))


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None

    value = value.strip()

    if not re.fullmatch(r"[+-]?\d+", value):
        return None

    try:
        return int(value)
    except ValueError:
        return None


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None

    try:
        result = float(value.strip())
    except ValueError:
        return None

    return result if math.isfinite(result) else None


def child_int(
    element: ET.Element,
    child_name: str,
) -> int | None:
    return parse_int(child_text(element, child_name))


def child_positive_int(
    element: ET.Element,
    child_name: str,
) -> int | None:
    value = child_int(element, child_name)

    if value is None or value <= 0:
        return None

    return value


def child_float(
    element: ET.Element,
    child_name: str,
) -> float | None:
    return parse_float(child_text(element, child_name))


def snake_to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(
        part[:1].upper() + part[1:]
        for part in parts[1:]
    )


def dataclass_to_camel_case_dict(
    value: Any,
) -> Any:
    """
    Convert dataclasses into JSON-compatible dictionaries while changing
    Python snake_case field names to the camelCase schema used by Notestream.
    """
    if hasattr(value, "__dataclass_fields__"):
        result: dict[str, Any] = {}

        for key, item in asdict(value).items():
            if item is None:
                continue

            result[snake_to_camel(key)] = (
                dataclass_to_camel_case_dict(item)
            )

        return result

    if isinstance(value, dict):
        return {
            snake_to_camel(str(key)): dataclass_to_camel_case_dict(item)
            for key, item in value.items()
            if item is not None
        }

    if isinstance(value, list):
        return [
            dataclass_to_camel_case_dict(item)
            for item in value
        ]

    return value


if __name__ == "__main__":
    import argparse

    argument_parser = argparse.ArgumentParser(
        description=(
            "Convert first-version score-partwise MusicXML "
            "into Notestream JSON."
        )
    )

    argument_parser.add_argument(
        "input",
        type=Path,
        help="Path to the input .musicxml or .xml file.",
    )

    argument_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help=(
            "Optional output JSON path. If omitted, JSON is "
            "printed to stdout."
        ),
    )

    args = argument_parser.parse_args()

    converted = convert_musicxml_file(
        input_path=args.input,
        output_path=args.output,
    )

    if args.output is None:
        print(
            json.dumps(
                converted,
                indent=2,
                ensure_ascii=False,
            )
        )
