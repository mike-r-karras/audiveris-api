import sys
import os
import shutil
import tempfile
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Insert the app directory into sys.path to allow imports to work from anywhere
sys.path.insert(0, str(Path(__file__).parent))

import pytest
from fastapi.testclient import TestClient

from main import app, jobs, run_audiveris, ConversionJob
from chord_chart_parser import parse_chord_chart
from pdf_preflight import PreflightResult, classify_evidence
from pdf_source_layout import parse_bbox_layout

client = TestClient(app)

MINIMAL_VALID_MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1">
      <part-name>Music</part-name>
    </score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key>
          <fifths>0</fifths>
        </key>
        <time>
          <beats>4</beats>
          <beat-type>4</beat-type>
        </time>
        <clef>
          <sign>G</sign>
          <line>2</line>
        </clef>
      </attributes>
      <note>
        <pitch>
          <step>C</step>
          <octave>4</octave>
        </pitch>
        <duration>1</duration>
        <voice>1</voice>
        <type>quarter</type>
      </note>
    </measure>
  </part>
</score-partwise>
"""


class MockStream:
    def __init__(self, lines):
        self.lines = lines
        self.iter = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.iter)
        except StopIteration:
            raise StopAsyncIteration


class MockProcess:
    def __init__(self, lines, return_code=0):
        self.stdout = MockStream(lines)
        self.return_code = return_code

    async def wait(self):
        return self.return_code


@pytest.fixture(autouse=True)
def clean_jobs_and_output():
    # Clear the jobs dict
    jobs.clear()
    
    # Clean the app/output directory before/after each test
    output_dir = Path(__file__).parent / "output"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    yield
    
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_deployed_frontend_cors_preflight():
    response = client.options(
        "/conversions",
        headers={
            "Origin": "https://notestream.mike-r-karras.workers.dev",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "https://notestream.mike-r-karras.workers.dev"
    )


def test_get_conversion_not_found():
    response = client.get("/conversions/nonexistent-id")
    assert response.status_code == 404
    assert response.json()["detail"] == "Conversion not found"


def test_get_conversion_result_not_found():
    response = client.get("/conversions/nonexistent-id/result")
    assert response.status_code == 404
    assert response.json()["detail"] == "Conversion not found"


def test_create_conversion():
    # Create a dummy pdf file content
    file_content = b"%PDF-1.4 mock pdf content"
    files = {"file": ("test_score.pdf", file_content, "application/pdf")}
    
    with patch("main.run_audiveris") as mock_run:
        response = client.post("/conversions", files=files)
        assert response.status_code == 202
        data = response.json()
        assert "jobId" in data
        assert data["status"] == "queued"
        
        job_id = data["jobId"]
        assert job_id in jobs
        assert jobs[job_id].status in ("queued", "processing", "completed")
        mock_run.assert_called_once()


def test_preflight_detects_ukulele_chord_lyric_chart():
    text = """
    Stand By Me
    A . . . | A . . . | F#m . . . | F#m . . .
    When the night has come and the land is dark
    And the moon is the only light we'll see
    San Jose Ukulele Club
    """

    result = classify_evidence(text=text, staff_systems=0)

    assert result.sheet_type == "chord-lyrics"
    assert result.confidence >= 0.8
    assert result.instrument_candidates[0].instrument == "ukulele"
    assert any("beat dots" in item for item in result.evidence)


def test_preflight_prefers_standard_notation_when_staves_are_present():
    result = classify_evidence(
        text="Amazing Grace how sweet the sound",
        staff_systems=3,
    )

    assert result.sheet_type == "standard-notation"
    assert result.confidence >= 0.9


def test_preflight_strong_chord_chart_evidence_overrides_one_false_staff():
    text = """
    I Will
    C . . . | Am . . . | F . . . | G7 . . .
    C . . . | Am . . . | Dm . . . | G7 . . .
    Who knows how long I've loved you
    You know I love you still
    Ukulele
    """

    result = classify_evidence(text=text, staff_systems=1)

    assert result.sheet_type == "chord-lyrics"
    assert result.confidence >= 0.8
    assert any("five-line staff" in item for item in result.evidence)


def test_preflight_keeps_weak_evidence_unknown():
    result = classify_evidence(text="Untitled document", staff_systems=0)

    assert result.sheet_type == "unknown"


@pytest.mark.asyncio
async def test_run_audiveris_routes_chord_sheet_without_omr():
    job_id = "test-chord-sheet-job-id"
    jobs[job_id] = ConversionJob(
        jobId=job_id,
        status="queued",
        progress=0,
        stage="queued",
        message="Conversion queued",
    )
    detected = PreflightResult(
        sheet_type="chord-lyrics",
        confidence=0.94,
        evidence=["Chord symbols and repeated beat dots share rows"],
        instrument_candidates=[],
        extracted_text=True,
        staff_systems=0,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        input_path = tmp_path / "chart.pdf"
        input_path.write_bytes(b"%PDF-1.4 dummy")

        with (
            patch("main.classify_pdf", return_value=detected),
            patch(
                "main.extract_pdf_source_layout",
                return_value={"schemaVersion": "pdf-source-layout-1.0"},
            ),
            patch("asyncio.create_subprocess_exec") as subprocess_mock,
        ):
            await run_audiveris(job_id, input_path, tmp_path / "output")

    subprocess_mock.assert_not_called()
    assert jobs[job_id].status == "completed"
    assert jobs[job_id].preflight["sheetType"] == "chord-lyrics"
    assert Path(jobs[job_id].sourceLayoutPath).exists()
    assert Path(jobs[job_id].resultPath).exists()
    assert jobs[job_id].error is None
    response = client.get(f"/conversions/{job_id}/result")
    assert response.status_code == 200
    assert response.json()["schemaVersion"] == "chord-chart-1.0"


def test_parse_bbox_layout_preserves_stable_word_geometry():
    bbox = b"""<?xml version="1.0"?>
    <html xmlns="http://www.w3.org/1999/xhtml">
      <head><meta name="Author" content="Gillian"/></head>
      <body><doc><page width="612" height="792"><flow><block
        xMin="45" yMin="148" xMax="120" yMax="160"><line
        xMin="45" yMin="148" xMax="120" yMax="160">
        <word xMin="45" yMin="148" xMax="53" yMax="160">A</word>
        <word xMin="60" yMin="148" xMax="63" yMax="160">.</word>
      </line></block></flow></page></doc></body>
    </html>"""

    layout = parse_bbox_layout(bbox, source_filename="chart.pdf")

    assert layout["schemaVersion"] == "pdf-source-layout-1.0"
    assert layout["metadata"]["Author"] == "Gillian"
    page = layout["pages"][0]
    assert page["width"] == 612
    assert page["lines"][0]["wordIds"] == ["p1-w1", "p1-w2"]
    assert page["words"][0] == {
        "id": "p1-w1",
        "text": "A",
        "box": {"xMin": 45.0, "yMin": 148.0, "xMax": 53.0, "yMax": 160.0},
    }


def test_stand_by_me_golden_fixture_encodes_pickup_and_lyric_beats():
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "stand-by-me-pickup.golden.json"
    )
    chart = json.loads(fixture_path.read_text(encoding="utf-8"))
    measures = {
        measure["number"]: measure
        for section in chart["sections"]
        for measure in section["measures"]
    }

    assert chart["schemaVersion"] == "chord-chart-1.0"
    assert measures[8]["effectiveChord"] == "A"
    assert measures[8]["chords"] == []
    assert measures[8]["lyricCues"][0]["text"] == "When the"
    assert measures[8]["lyricCues"][0]["beat"] == {
        "numerator": 3,
        "denominator": 1,
    }
    assert [(cue["text"], cue["beat"]["numerator"]) for cue in measures[9]["lyricCues"]] == [
        ("night", 0),
        ("has", 3),
    ]
    assert measures[10]["chords"] == []
    assert measures[10]["effectiveChord"] == "A"
    assert measures[10]["lyricCues"][0]["text"] == "come,"


def test_spatial_parser_matches_stand_by_me_pickup_fixture():
    words = []

    def add(word_id, text, x, y):
        words.append({
            "id": word_id,
            "text": text,
            "box": {"xMin": x, "yMin": y, "xMax": x + max(3, len(text) * 6), "yMax": y + 11},
        })

    intro = [
        ("p1-w16", "Intro:"), ("p1-w17", "A"), ("p1-w18", "."), ("p1-w19", "."), ("p1-w20", "."),
        ("p1-w21", "|"), ("p1-w22", "."), ("p1-w23", "."), ("p1-w24", "."), ("p1-w25", "."),
        ("p1-w26", "|F#m"), ("p1-w27", "."), ("p1-w28", "."), ("p1-w29", "."),
        ("p1-w30", "|"), ("p1-w31", "."), ("p1-w32", "."), ("p1-w33", "."), ("p1-w34", "."),
        ("p1-w35", "|D"), ("p1-w36", "."), ("p1-w37", "."), ("p1-w38", "."),
        ("p1-w39", "|"), ("p1-w40", "E"), ("p1-w41", "."), ("p1-w42", "."), ("p1-w43", "."),
        ("p1-w44", "|"), ("p1-w45", "A"), ("p1-w46", "."), ("p1-w47", "."), ("p1-w48", "."),
        ("p1-w49", "|"), ("p1-w50", "."), ("p1-w51", "."), ("p1-w52", "."),
    ]
    for index, (word_id, text) in enumerate(intro):
        add(word_id, text, 45 + index * 10, 149)

    for word_id, text, x in [
        ("p1-w53", ".", 45), ("p1-w54", "A", 108), ("p1-w55", ".", 126),
        ("p1-w56", ".", 136), ("p1-w57", ".", 146), ("p1-w58", "|", 159),
        ("p1-w59", ".", 172), ("p1-w60", ".", 196), ("p1-w61", ".", 206),
        ("p1-w62", ".", 219), ("p1-w63", "|", 226), ("p1-w64", "F#m", 232),
        ("p1-w65", ".", 263), ("p1-w66", ".", 313), ("p1-w67", ".", 333),
    ]:
        add(word_id, text, x, 176)

    for word_id, text, x in [
        ("p1-w72", "When", 45), ("p1-w73", "the", 79), ("p1-w74", "night", 99),
        ("p1-w75", "has", 142), ("p1-w76", "come,", 165),
    ]:
        add(word_id, text, x, 190)

    layout = {
        "sourceFilename": "Stand By Me (original key of A).pdf",
        "pages": [{"number": 1, "words": words}],
    }
    chart = parse_chord_chart(layout, instrument="ukulele")
    measures = {
        measure["number"]: measure
        for section in chart["sections"]
        for measure in section["measures"]
    }

    assert measures[8]["effectiveChord"] == "A"
    assert measures[8]["lyricCues"][0]["text"] == "When the"
    assert measures[8]["lyricCues"][0]["role"] == "pickup"
    assert [(cue["text"], cue["beat"]["numerator"]) for cue in measures[9]["lyricCues"]] == [
        ("night", 0), ("has", 3)
    ]
    assert measures[10]["effectiveChord"] == "A"
    assert measures[10]["chords"] == []
    assert measures[10]["lyricCues"][0]["text"] == "come,"


def test_spatial_parser_rejoins_split_chord_suffixes():
    words = []
    for index, text in enumerate(["|", "C", "\\", "C", "7\\", "G", "m", ".", "|"]):
        words.append({
            "id": f"p1-w{index + 1}",
            "text": text,
            "box": {
                "xMin": 40 + index * 12,
                "yMin": 100,
                "xMax": 48 + index * 12,
                "yMax": 112,
            },
        })

    chart = parse_chord_chart({
        "sourceFilename": "split-suffixes.pdf",
        "pages": [{"number": 1, "words": words}],
    })
    measure = chart["sections"][0]["measures"][0]

    assert [chord["symbol"] for chord in measure["chords"]] == ["C", "C7", "Gm"]
    assert measure["chords"][1]["sourceRef"]["wordIds"] == ["p1-w4", "p1-w5"]
    assert measure["chords"][2]["sourceRef"]["wordIds"] == ["p1-w6", "p1-w7"]


def test_spatial_parser_rejoins_split_flat_and_lowercase_slash_bass():
    words = []
    for index, text in enumerate(
        ["|", "A", "m/c", ".", ".", ".", "|", "B", "b", ".", ".", ".", "|"]
    ):
        words.append({
            "id": f"p1-w{index + 1}",
            "text": text,
            "box": {
                "xMin": 40 + index * 12,
                "yMin": 100,
                "xMax": 48 + index * 12,
                "yMax": 112,
            },
        })

    chart = parse_chord_chart({
        "sourceFilename": "split-flat-and-slash.pdf",
        "pages": [{"number": 1, "words": words}],
    })
    measures = chart["sections"][0]["measures"]

    assert measures[0]["effectiveChord"] == "Am/C"
    assert measures[0]["sourceRef"]["wordIds"][:2] == ["p1-w2", "p1-w3"]
    assert measures[1]["effectiveChord"] == "Bb"
    assert measures[1]["sourceRef"]["wordIds"][:2] == ["p1-w8", "p1-w9"]


def test_spatial_parser_keeps_lyrics_on_section_label_row():
    words = [
        {
            "id": "p1-w1",
            "text": text,
            "box": {
                "xMin": x,
                "yMin": y,
                "xMax": x + max(3, len(text) * 6),
                "yMax": y + 11,
            },
        }
        for text, x, y in [
            ("|", 40, 100),
            ("A", 70, 100),
            (".", 90, 100),
            (".", 120, 100),
            (".", 150, 100),
            ("|", 180, 100),
            ("Chorus:", 40, 113),
            ("So", 50, 113),
            ("dar-lin’", 90, 113),
        ]
    ]
    for index, word in enumerate(words, start=1):
        word["id"] = f"p1-w{index}"

    chart = parse_chord_chart({
        "sourceFilename": "section-lyrics.pdf",
        "pages": [{"number": 1, "words": words}],
    })
    section = chart["sections"][0]
    measure = section["measures"][0]

    assert section["label"] == "Chorus"
    assert [cue["text"] for cue in measure["lyricCues"]] == ["So", "dar-lin’"]
    assert all("Chorus:" not in cue["text"] for cue in measure["lyricCues"])


def test_spatial_parser_keeps_leading_words_near_measure_downbeats():
    words = []

    def add(word_id, text, x, y):
        words.append({
            "id": word_id,
            "text": text,
            "box": {
                "xMin": x,
                "yMin": y,
                "xMax": x + max(3, len(text) * 6),
                "yMax": y + 11,
            },
        })

    for word_id, text, x in [
        ("p1-w1", "Am", 31.5),
        ("p1-w2", ".", 85.7),
        ("p1-w3", ".", 132.3),
        ("p1-w4", ".", 155.7),
        ("p1-w5", "|", 171.3),
        ("p1-w6", "E7", 282.6),
        ("p1-w7", ".", 329.6),
        ("p1-w8", ".", 376.4),
        ("p1-w9", ".", 403.6),
        ("p1-w10", "|", 436.8),
    ]:
        add(word_id, text, x, 642.5)

    for word_id, text, x in [
        ("p1-w11", "Her", 54.9),
        ("p1-w12", "mind", 81.3),
        ("p1-w13", "is", 115.5),
        ("p1-w14", "Tiffany-twisted", 129.5),
        ("p1-w15", "She", 299.2),
        ("p1-w16", "got", 327.9),
        ("p1-w17", "the", 349.6),
        ("p1-w18", "Mercedes", 373.0),
        ("p1-w19", "bends", 447.0),
    ]:
        add(word_id, text, x, 656.8)

    chart = parse_chord_chart({
        "sourceFilename": "downbeat-leading-lyrics.pdf",
        "pages": [{"number": 1, "words": words}],
    })
    measures = chart["sections"][0]["measures"]

    assert " ".join(cue["text"] for cue in measures[0]["lyricCues"]) == (
        "Her mind is Tiffany-twisted"
    )
    assert " ".join(cue["text"] for cue in measures[1]["lyricCues"]) == (
        "She got the Mercedes bends"
    )
    assert measures[0]["lyricCues"][0]["sourceRef"]["wordIds"] == ["p1-w11"]
    assert measures[1]["lyricCues"][0]["sourceRef"]["wordIds"] == ["p1-w15"]


@pytest.mark.asyncio
async def test_run_audiveris_success():
    job_id = "test-success-job-id"
    jobs[job_id] = ConversionJob(
        jobId=job_id,
        status="queued",
        progress=0,
        stage="queued",
        message="Conversion queued",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        input_path = tmp_path / "test_score.pdf"
        input_path.write_bytes(b"%PDF-1.4 dummy")
        output_dir = tmp_path / "output"

        # We mock create_subprocess_exec to write a dummy MusicXML file to output_dir
        # when called, simulating Audiveris OMR output.
        lines = [
            b"STEP 1: Document preparation",
            b"STEP 2: Layout analysis",
            b"Exporting MusicXML..."
        ]
        
        async def mock_create_subprocess_exec(*args, **kwargs):
            # Create the output directory and write the MusicXML file
            output_dir.mkdir(parents=True, exist_ok=True)
            musicxml_file = output_dir / "test_score.musicxml"
            musicxml_file.write_text(MINIMAL_VALID_MUSICXML, encoding="utf-8")
            return MockProcess(lines, return_code=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess_exec):
            await run_audiveris(job_id, input_path, output_dir)

        # Assert job state
        job = jobs[job_id]
        assert job.status == "completed"
        assert job.progress == 100
        assert job.stage == "completed"
        assert job.error is None

        # Assert files created in app/output
        app_output_dir = Path(__file__).parent / "output"
        copied_musicxml = app_output_dir / "test_score.musicxml"
        copied_ezs = app_output_dir / "test_score.ezs"

        assert copied_musicxml.exists()
        assert copied_ezs.exists()

        # Verify the easyScore JSON structure
        ezs_data = json.loads(copied_ezs.read_text(encoding="utf-8"))
        assert ezs_data["schemaVersion"] == "1.2"
        assert ezs_data["sourceFormat"] == "musicxml"
        assert "parts" in ezs_data
        assert len(ezs_data["parts"]) == 1

        # Test retrieving the result via client
        response = client.get(f"/conversions/{job_id}/result")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.headers["content-disposition"] == 'attachment; filename="test_score.ezs"'
        
        # Verify result contents
        result_json = response.json()
        assert result_json["schemaVersion"] == "1.2"


@pytest.mark.asyncio
async def test_run_audiveris_subprocess_failure():
    job_id = "test-failure-job-id"
    jobs[job_id] = ConversionJob(
        jobId=job_id,
        status="queued",
        progress=0,
        stage="queued",
        message="Conversion queued",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        input_path = tmp_path / "test_score.pdf"
        input_path.write_bytes(b"%PDF-1.4 dummy")
        output_dir = tmp_path / "output"

        async def mock_create_subprocess_exec(*args, **kwargs):
            return MockProcess([b"Error: Failed to process document"], return_code=1)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess_exec):
            await run_audiveris(job_id, input_path, output_dir)

        # Assert job state is failed
        job = jobs[job_id]
        assert job.status == "failed"
        assert job.stage == "failed"
        assert "Audiveris exited with status code 1" in job.error


@pytest.mark.asyncio
async def test_get_conversion_long_poll():
    # Set up a processing job
    job_id = "long-poll-job-id"
    jobs[job_id] = ConversionJob(
        jobId=job_id,
        status="processing",
        progress=50,
        stage="recognizing",
        message="Running OMR",
    )
    
    async def update_job_later():
        await asyncio.sleep(0.1)
        jobs[job_id].status = "completed"
        jobs[job_id].progress = 100
        jobs[job_id].stage = "completed"
        jobs[job_id].message = "Conversion completed"

    task = asyncio.create_task(update_job_later())
    
    from main import get_conversion
    job = await get_conversion(job_id, timeout=2)
    assert job.status == "completed"
    assert job.progress == 100
    
    await task


@pytest.mark.asyncio
async def test_run_musicxml_conversion_success():
    job_id = "test-musicxml-job-id"
    jobs[job_id] = ConversionJob(
        jobId=job_id,
        status="queued",
        progress=0,
        stage="queued",
        message="Conversion queued",
    )
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        input_path = tmp_path / "test_score.musicxml"
        input_path.write_text(MINIMAL_VALID_MUSICXML, encoding="utf-8")
        
        from main import run_musicxml_conversion
        await run_musicxml_conversion(job_id, input_path)
        
        job = jobs[job_id]
        assert job.status == "completed"
        assert job.progress == 100
        assert Path(job.resultPath).exists()


def test_create_conversion_mxl_compressed():
    import io
    import zipfile
    
    mxl_io = io.BytesIO()
    with zipfile.ZipFile(mxl_io, "w") as archive:
        container_xml = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<container version=\"1.0\" xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">\n<rootfiles>\n<rootfile full-path=\"mock.musicxml\" media-type=\"application/vnd.recordare.musicxml+xml\"/>\n</rootfiles>\n</container>\n"
        archive.writestr("META-INF/container.xml", container_xml)
        archive.writestr("mock.musicxml", MINIMAL_VALID_MUSICXML)
    
    file_content = mxl_io.getvalue()
    files = {"file": ("test_score.mxl", file_content, "application/octet-stream")}
    
    response = client.post("/conversions", files=files)
    assert response.status_code == 202
    data = response.json()
    assert "jobId" in data
    assert data["status"] == "queued"
    
    job_id = data["jobId"]
    assert job_id in jobs
    assert jobs[job_id].status in ("queued", "processing", "completed")


def test_converter_supports_multistaff_multivoice_backup_forward():
    from musicxml_converter import convert_musicxml

    piano_xml = """<?xml version="1.0"?>
    <score-partwise version="4.0">
      <part-list>
        <score-part id="P1"><part-name>Piano</part-name></score-part>
      </part-list>
      <part id="P1">
        <measure number="1">
          <attributes>
            <divisions>2</divisions>
            <time><beats>4</beats><beat-type>4</beat-type></time>
            <staves>2</staves>
            <clef number="1"><sign>G</sign><line>2</line></clef>
            <clef number="2"><sign>F</sign><line>4</line></clef>
          </attributes>
          <note>
            <pitch><step>C</step><octave>5</octave></pitch>
            <duration>2</duration><voice>1</voice><staff>1</staff>
            <type>quarter</type>
          </note>
          <note>
            <pitch><step>E</step><octave>5</octave></pitch>
            <duration>2</duration><voice>2</voice><staff>1</staff>
            <type>quarter</type>
          </note>
          <backup><duration>4</duration></backup>
          <note>
            <pitch><step>C</step><octave>3</octave></pitch>
            <duration>4</duration><voice>5</voice><staff>2</staff>
            <type>half</type>
          </note>
          <forward><duration>2</duration></forward>
          <note>
            <rest/><duration>2</duration><voice>5</voice><staff>2</staff>
            <type>quarter</type>
          </note>
        </measure>
      </part>
    </score-partwise>"""

    result = convert_musicxml(piano_xml)
    measure = result["parts"][0]["measures"][0]

    assert measure["attributes"]["clef"]["sign"] == "G"
    assert measure["attributes"]["clefs"]["1"]["sign"] == "G"
    assert measure["attributes"]["clefs"]["2"]["sign"] == "F"

    voices = {
        (voice["staff"], voice["number"]): voice
        for voice in measure["voices"]
    }

    assert set(voices) == {(1, 1), (1, 2), (2, 5)}
    assert voices[(1, 1)]["events"][0]["startDivisions"] == 0
    assert voices[(1, 2)]["events"][0]["startDivisions"] == 2
    assert voices[(2, 5)]["events"][0]["startDivisions"] == 0
    assert voices[(2, 5)]["events"][1]["startDivisions"] == 6

    warning_codes = {warning["code"] for warning in result["warnings"]}
    assert "VOICE_IGNORED" not in warning_codes
    assert "STAFF_IGNORED" not in warning_codes
    assert "COMPLEX_MEASURE_TIMING" not in warning_codes


def test_converter_preserves_all_parts_and_piano_accompaniment():
    from musicxml_converter import convert_musicxml

    xml = """<?xml version="1.0"?>
    <score-partwise version="4.0">
      <part-list>
        <score-part id="P1"><part-name>Voice</part-name></score-part>
        <score-part id="P2"><part-name>Piano</part-name></score-part>
      </part-list>
      <part id="P1">
        <measure number="1">
          <attributes>
            <divisions>1</divisions>
            <time><beats>3</beats><beat-type>4</beat-type></time>
            <clef><sign>G</sign><line>2</line></clef>
          </attributes>
          <note>
            <pitch><step>C</step><octave>5</octave></pitch>
            <duration>1</duration><voice>1</voice><staff>1</staff>
            <type>quarter</type>
            <lyric number="1"><syllabic>single</syllabic><text>O</text></lyric>
          </note>
        </measure>
      </part>
      <part id="P2">
        <measure number="1">
          <attributes>
            <divisions>2</divisions>
            <time><beats>3</beats><beat-type>4</beat-type></time>
            <staves>2</staves>
            <clef number="1"><sign>G</sign><line>2</line></clef>
            <clef number="2"><sign>F</sign><line>4</line></clef>
          </attributes>
          <direction><sound tempo="96"/></direction>
          <note>
            <pitch><step>E</step><octave>4</octave></pitch>
            <duration>6</duration><voice>1</voice><staff>1</staff><type>half</type>
          </note>
          <backup><duration>6</duration></backup>
          <note>
            <pitch><step>C</step><octave>3</octave></pitch>
            <duration>6</duration><voice>2</voice><staff>2</staff><type>half</type>
          </note>
        </measure>
      </part>
    </score-partwise>"""

    result = convert_musicxml(xml)

    assert [(part["id"], part["name"]) for part in result["parts"]] == [
        ("P1", "Voice"),
        ("P2", "Piano"),
    ]
    voice_event = result["parts"][0]["measures"][0]["voices"][0]["events"][0]
    assert voice_event["lyrics"][0]["text"] == "O"

    piano_measure = result["parts"][1]["measures"][0]
    assert piano_measure["attributes"]["divisions"] == 2
    assert piano_measure["attributes"]["clefs"]["2"]["sign"] == "F"
    assert {(voice["staff"], voice["number"]) for voice in piano_measure["voices"]} == {
        (1, 1),
        (2, 2),
    }
    piano_event_ids = {
        event["id"]
        for voice in piano_measure["voices"]
        for event in voice["events"]
    }
    assert all(event_id.startswith("P2-") for event_id in piano_event_ids)
    assert result["metadata"]["bpm"] == 96.0
    assert "MULTIPLE_PARTS_IGNORED" not in {
        warning["code"] for warning in result.get("warnings", [])
    }


def test_converter_auto_normalizes_terminal_overfull_note():
    from musicxml_converter import convert_musicxml

    xml = """<?xml version="1.0"?>
    <score-partwise version="4.0">
      <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
      <part id="P1">
        <measure number="16">
          <attributes>
            <divisions>4</divisions>
            <time><beats>3</beats><beat-type>8</beat-type></time>
          </attributes>
          <note><pitch><step>A</step><octave>4</octave></pitch><duration>5</duration><voice>1</voice><type>quarter</type></note>
          <note><pitch><step>B</step><octave>4</octave></pitch><duration>2</duration><voice>1</voice><type>eighth</type></note>
        </measure>
      </part>
    </score-partwise>"""

    result = convert_musicxml(xml)
    event = result["parts"][0]["measures"][0]["voices"][0]["events"][-1]

    assert event["startQuarterNotes"] == 1.25
    assert event["duration"]["quarterNotes"] == 0.25
    assert event["duration"]["vexflow"] == "16"
    assert event["normalization"]["type"] == "trim-at-measure-boundary"
    assert event["normalization"]["confidence"] == 0.96
    assert event["normalization"]["originalDuration"]["quarterNotes"] == 0.5

    warning_codes = {warning["code"] for warning in result["warnings"]}
    assert "MEASURE_AUTO_NORMALIZED" in warning_codes
    assert "MEASURE_OVERFULL" not in warning_codes


def test_converter_does_not_auto_normalize_tied_terminal_note():
    from musicxml_converter import convert_musicxml

    xml = """<?xml version="1.0"?>
    <score-partwise version="4.0">
      <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
      <part id="P1">
        <measure number="16">
          <attributes>
            <divisions>4</divisions>
            <time><beats>3</beats><beat-type>8</beat-type></time>
          </attributes>
          <note><pitch><step>A</step><octave>4</octave></pitch><duration>5</duration><voice>1</voice><type>quarter</type></note>
          <note>
            <pitch><step>B</step><octave>4</octave></pitch>
            <duration>2</duration><voice>1</voice><type>eighth</type>
            <tie type="start"/><notations><tied type="start"/></notations>
          </note>
        </measure>
      </part>
    </score-partwise>"""

    result = convert_musicxml(xml)
    event = result["parts"][0]["measures"][0]["voices"][0]["events"][-1]

    assert event["duration"]["quarterNotes"] == 0.5
    assert "normalization" not in event

    warning_codes = {warning["code"] for warning in result["warnings"]}
    assert "MEASURE_AUTO_NORMALIZED" not in warning_codes
    assert "MEASURE_OVERFULL" in warning_codes


def test_musicxml_metadata_and_tempo_events():
    from musicxml_converter import convert_musicxml

    xml = '''<?xml version="1.0" encoding="UTF-8"?>
    <score-partwise version="4.0">
      <work><work-title>Example Song</work-title></work>
      <movement-title>First Movement</movement-title>
      <identification>
        <creator type="composer">Composer Name</creator>
        <creator type="arranger">Arranger Name</creator>
        <creator type="lyricist">Lyricist Name</creator>
        <rights>Copyright 2026 Example</rights>
        <miscellaneous>
          <miscellaneous-field name="year">1810</miscellaneous-field>
          <miscellaneous-field name="genre">Classical</miscellaneous-field>
        </miscellaneous>
      </identification>
      <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
      <part id="P1">
        <measure number="1">
          <attributes><divisions>2</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
          <direction>
            <direction-type>
              <words>Poco moto</words>
              <metronome><beat-unit>quarter</beat-unit><per-minute>72</per-minute></metronome>
            </direction-type>
            <sound tempo="72"/>
          </direction>
          <note><rest/><duration>8</duration><voice>1</voice><type>whole</type></note>
        </measure>
        <measure number="2">
          <direction>
            <direction-type><words>Più mosso</words></direction-type>
            <sound tempo="96"/>
          </direction>
          <note><rest/><duration>8</duration><voice>1</voice><type>whole</type></note>
        </measure>
      </part>
    </score-partwise>'''

    result = convert_musicxml(xml)
    metadata = result["metadata"]
    assert result["schemaVersion"] == "1.2"
    assert metadata == {
        "title": "Example Song",
        "movementTitle": "First Movement",
        "composer": "Composer Name",
        "arranger": "Arranger Name",
        "lyricist": "Lyricist Name",
        "copyright": "Copyright 2026 Example",
        "year": 1810,
        "genre": "Classical",
        "bpm": 72.0,
        "tempoText": "Poco moto",
    }
    assert result["parts"][0]["measures"][0]["tempoEvents"][0]["bpm"] == 72.0
    assert result["parts"][0]["measures"][1]["tempoEvents"][0]["text"] == "Più mosso"
    assert result["parts"][0]["measures"][1]["tempoEvents"][0]["bpm"] == 96.0


LYRIC_MUSICXML = '''<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <staves>2</staves>
      </attributes>
      <note>
        <pitch><step>C</step><octave>5</octave></pitch>
        <duration>1</duration><voice>1</voice><staff>1</staff><type>quarter</type>
        <lyric number="1" name="English" placement="below">
          <syllabic>begin</syllabic><text>Christ</text>
        </lyric>
        <lyric number="2"><syllabic>single</syllabic><text xml:space="preserve"> Ô </text></lyric>
      </note>
      <note>
        <pitch><step>E</step><octave>5</octave></pitch>
        <duration>1</duration><voice>1</voice><staff>1</staff><type>quarter</type>
        <lyric number="1">
          <syllabic>end</syllabic><text>mas</text><extend type="start"/>
        </lyric>
      </note>
      <note>
        <pitch><step>G</step><octave>5</octave></pitch>
        <duration>1</duration><voice>1</voice><staff>1</staff><type>quarter</type>
        <lyric number="1">
          <syllabic>single</syllabic><text>tree</text><elision>‿</elision><text>bright</text>
          <end-line/>
        </lyric>
      </note>
      <note>
        <rest/><duration>1</duration><voice>1</voice><staff>1</staff><type>quarter</type>
        <lyric number="1"><text>ignored</text></lyric>
      </note>
      <backup><duration>4</duration></backup>
      <note>
        <pitch><step>C</step><octave>3</octave></pitch>
        <duration>4</duration><voice>2</voice><staff>2</staff><type>whole</type>
      </note>
    </measure>
  </part>
</score-partwise>'''


def test_converter_preserves_note_attached_lyrics():
    from musicxml_converter import convert_musicxml

    result = convert_musicxml(LYRIC_MUSICXML)
    voices = result["parts"][0]["measures"][0]["voices"]
    melody = next(voice for voice in voices if voice["staff"] == 1)
    first, second, third, rest = melody["events"]

    assert first["lyrics"] == [
        {
            "number": "1",
            "text": "Christ",
            "name": "English",
            "syllabic": "begin",
            "placement": "below",
        },
        {"number": "2", "text": " Ô ", "syllabic": "single"},
    ]
    assert second["lyrics"] == [
        {
            "number": "1",
            "text": "mas",
            "syllabic": "end",
            "extend": "start",
        }
    ]
    assert third["lyrics"] == [
        {
            "number": "1",
            "text": "tree‿bright",
            "syllabic": "single",
            "elision": "‿",
            "endLine": True,
        }
    ]
    assert "lyrics" not in rest
    assert "LYRIC_ON_REST_IGNORED" in {
        warning["code"] for warning in result["warnings"]
    }


def test_converter_merges_chord_tone_lyrics_without_duplicates():
    from musicxml_converter import convert_musicxml

    xml = LYRIC_MUSICXML.replace(
        '<duration>1</duration><voice>1</voice><staff>1</staff><type>quarter</type>\n'
        '        <lyric number="1">\n'
        '          <syllabic>end</syllabic><text>mas</text><extend type="start"/>',
        '<chord/><duration>1</duration><voice>1</voice><staff>1</staff><type>quarter</type>\n'
        '        <lyric number="1">\n'
        '          <syllabic>begin</syllabic><text>Christ</text>',
        1,
    )

    result = convert_musicxml(xml)
    melody = next(
        voice
        for voice in result["parts"][0]["measures"][0]["voices"]
        if voice["staff"] == 1
    )
    chord = melody["events"][0]

    assert len(chord["pitches"]) == 2
    assert chord["lyrics"][0]["text"] == "Christ"
    assert len(chord["lyrics"]) == 2


def test_compressed_mxl_preserves_lyrics(tmp_path):
    import zipfile
    from musicxml_converter import convert_musicxml_file

    mxl_path = tmp_path / "lyrics.mxl"
    with zipfile.ZipFile(mxl_path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
              <rootfiles><rootfile full-path="score.musicxml"/></rootfiles>
            </container>''',
        )
        archive.writestr("score.musicxml", LYRIC_MUSICXML)

    result = convert_musicxml_file(mxl_path)
    melody = next(
        voice
        for voice in result["parts"][0]["measures"][0]["voices"]
        if voice["staff"] == 1
    )
    assert melody["events"][0]["lyrics"][0]["text"] == "Christ"
