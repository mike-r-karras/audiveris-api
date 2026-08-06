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
        assert ezs_data["schemaVersion"] == "1.0"
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
        assert result_json["schemaVersion"] == "1.0"


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
    assert result["schemaVersion"] == "1.1"
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
