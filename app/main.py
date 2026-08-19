import asyncio
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Literal
from datetime import datetime, timezone

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import musicxml_converter
from chord_chart_parser import parse_chord_chart
from pdf_preflight import PreflightResult, classify_pdf
from pdf_source_layout import extract_pdf_source_layout

app = FastAPI()

default_allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://notestream.mike-r-karras.workers.dev",
]
allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        ",".join(default_allowed_origins),
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

JobStatus = Literal["queued", "processing", "completed", "failed"]


class ConversionJob(BaseModel):
    jobId: str
    status: JobStatus
    progress: int
    stage: str
    message: str
    resultPath: str | None = None
    error: str | None = None
    preflight: dict[str, object] | None = None
    sourceLayoutPath: str | None = None


jobs: dict[str, ConversionJob] = {}


def update_job(
    job_id: str,
    *,
    progress: int,
    stage: str,
    message: str,
    status: JobStatus = "processing",
) -> None:
    job = jobs[job_id]
    job.progress = max(0, min(100, progress))
    job.stage = stage
    job.message = message
    job.status = status


async def run_audiveris(job_id: str, input_path: Path, output_dir: Path) -> None:
    try:
        update_job(
            job_id,
            progress=10,
            stage="preparing",
            message="Preparing uploaded score",
        )

        update_job(
            job_id,
            progress=12,
            stage="preflighting",
            message="Classifying uploaded PDF",
        )

        loop = asyncio.get_running_loop()
        preflight: PreflightResult = await loop.run_in_executor(
            None,
            classify_pdf,
            input_path,
        )
        jobs[job_id].preflight = preflight.to_dict()

        if preflight.sheet_type == "chord-lyrics" and preflight.confidence >= 0.8:
            update_job(
                job_id,
                progress=18,
                stage="extracting-source-layout",
                message="Preserving chord-chart text geometry",
            )
            source_layout = await loop.run_in_executor(
                None,
                extract_pdf_source_layout,
                input_path,
            )
            app_output_dir = Path(__file__).parent / "output"
            app_output_dir.mkdir(parents=True, exist_ok=True)
            source_layout_path = (
                app_output_dir / f"{input_path.stem}.source-layout.json"
            )
            source_layout_path.write_text(
                json.dumps(source_layout, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            jobs[job_id].sourceLayoutPath = str(source_layout_path)

            instrument_candidate = (
                preflight.instrument_candidates[0]
                if preflight.instrument_candidates
                else None
            )
            chart = await loop.run_in_executor(
                None,
                lambda: parse_chord_chart(
                    source_layout,
                    instrument=(
                        instrument_candidate.instrument
                        if instrument_candidate
                        else None
                    ),
                    instrument_evidence=(
                        instrument_candidate.evidence
                        if instrument_candidate
                        else None
                    ),
                ),
            )
            chart_path = app_output_dir / f"{input_path.stem}.ezs"
            chart_path.write_text(
                json.dumps(chart, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            jobs[job_id].resultPath = str(chart_path)
            update_job(
                job_id,
                progress=100,
                stage="completed",
                message="Chord-chart conversion completed",
                status="completed",
            )
            return

        output_dir.mkdir(parents=True, exist_ok=True)

        update_job(
            job_id,
            progress=20,
            stage="recognizing",
            message="Starting optical music recognition",
        )

        process = await asyncio.create_subprocess_exec(
            "/opt/audiveris/opt/audiveris/bin/Audiveris",
            "-batch",
            "-export",
            "-output",
            str(output_dir),
            str(input_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        assert process.stdout is not None

        # Audiveris does not expose a dependable 0–100 percentage.
        # Advance through an estimated range while processing its output.
        estimated_progress = 20

        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()

            if not line:
                continue

            lower_line = line.lower()

            if "load" in lower_line:
                estimated_progress = max(estimated_progress, 30)
                message = "Loading score pages"
            elif "step" in lower_line:
                estimated_progress = min(estimated_progress + 4, 80)
                message = "Analyzing musical notation"
            elif "export" in lower_line:
                estimated_progress = max(estimated_progress, 85)
                message = "Exporting MusicXML"
            else:
                estimated_progress = min(estimated_progress + 1, 80)
                message = "Recognizing musical symbols"

            update_job(
                job_id,
                progress=estimated_progress,
                stage="recognizing",
                message=message,
            )

        return_code = await process.wait()

        if return_code != 0:
            raise RuntimeError(
                f"Audiveris exited with status code {return_code}"
            )

        update_job(
            job_id,
            progress=90,
            stage="locating-output",
            message="Locating generated MusicXML",
        )

        candidates = (
            list(output_dir.rglob("*.mxl"))
            + list(output_dir.rglob("*.musicxml"))
            + list(output_dir.rglob("*.xml"))
        )

        if not candidates:
            raise RuntimeError("Audiveris did not produce a MusicXML file")

        result_path = candidates[0]

        # Save a copy of the MusicXML file to the app's output directory
        app_output_dir = Path(__file__).parent / "output"
        app_output_dir.mkdir(parents=True, exist_ok=True)

        update_job(
            job_id,
            progress=92,
            stage="saving-musicxml",
            message="Saving MusicXML copy to output directory",
        )
        dest_musicxml_path = app_output_dir / result_path.name
        shutil.copy(result_path, dest_musicxml_path)

        # Convert to easyScore format
        update_job(
            job_id,
            progress=95,
            stage="converting-easyscore",
            message="Converting MusicXML to easyScore",
        )
        filename = input_path.stem
        ezs_path = app_output_dir / f"{filename}.ezs"

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            musicxml_converter.convert_musicxml_file,
            result_path,
            ezs_path,
        )

        job = jobs[job_id]
        job.resultPath = str(ezs_path)

        update_job(
            job_id,
            progress=100,
            stage="completed",
            message="Conversion completed",
            status="completed",
        )

    except Exception as exc:
        job = jobs[job_id]
        job.status = "failed"
        job.stage = "failed"
        job.message = "Conversion failed"
        job.error = str(exc)


async def run_musicxml_conversion(job_id: str, input_path: Path) -> None:
    try:
        update_job(
            job_id,
            progress=20,
            stage="preparing",
            message="Preparing uploaded MusicXML",
        )
        await asyncio.sleep(0.1)

        app_output_dir = Path(__file__).parent / "output"
        app_output_dir.mkdir(parents=True, exist_ok=True)

        update_job(
            job_id,
            progress=50,
            stage="converting-easyscore",
            message="Converting MusicXML to easyScore",
        )

        ezs_path = app_output_dir / f"{input_path.stem}.ezs"

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            musicxml_converter.convert_musicxml_file,
            input_path,
            ezs_path,
        )

        job = jobs[job_id]
        job.resultPath = str(ezs_path)

        update_job(
            job_id,
            progress=100,
            stage="completed",
            message="Conversion completed",
            status="completed",
        )

    except Exception as exc:
        job = jobs[job_id]
        job.status = "failed"
        job.stage = "failed"
        job.message = "Conversion failed"
        job.error = str(exc)


@app.post("/conversions", status_code=202)
async def create_conversion(file: UploadFile = File(...)):
    filename = file.filename or "score.pdf"
    lower_filename = filename.lower()

    is_pdf = lower_filename.endswith(".pdf")
    is_xml = lower_filename.endswith((".xml", ".musicxml", ".mxl"))

    if not is_pdf and not is_xml:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file format. Please upload PDF, XML, MusicXML, or MXL."
        )

    job_id = str(uuid.uuid4())
    work_dir = Path(tempfile.mkdtemp(prefix=f"conversion-{job_id}-"))
    input_path = work_dir / filename
    output_dir = work_dir / "output"

    with input_path.open("wb") as destination:
        shutil.copyfileobj(file.file, destination)

    jobs[job_id] = ConversionJob(
        jobId=job_id,
        status="queued",
        progress=0,
        stage="queued",
        message="Conversion queued",
    )

    if is_pdf:
        asyncio.create_task(
            run_audiveris(
                job_id=job_id,
                input_path=input_path,
                output_dir=output_dir,
            )
        )
    else:
        asyncio.create_task(
            run_musicxml_conversion(
                job_id=job_id,
                input_path=input_path,
            )
        )

    return {
        "jobId": job_id,
        "status": "queued",
        "progressUrl": f"/conversions/{job_id}",
        "resultUrl": f"/conversions/{job_id}/result",
    }


@app.get("/conversions/{job_id}", response_model=ConversionJob)
async def get_conversion(job_id: str, timeout: int = 30):
    job = jobs.get(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Conversion not found")

    if timeout <= 0 or job.status in ("completed", "failed"):
        return job

    start_time = asyncio.get_running_loop().time()
    while job.status not in ("completed", "failed"):
        elapsed = asyncio.get_running_loop().time() - start_time
        if elapsed >= timeout:
            break
        await asyncio.sleep(0.5)
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Conversion not found")

    return job


@app.get("/conversions/{job_id}/result")
async def get_conversion_result(job_id: str):
    job = jobs.get(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Conversion not found")

    if job.status == "failed":
        raise HTTPException(
            status_code=422,
            detail=job.error or "Conversion failed",
        )

    if job.status != "completed" or not job.resultPath:
        raise HTTPException(
            status_code=409,
            detail="Conversion is not complete",
        )

    if not os.path.exists(job.resultPath):
        raise HTTPException(
            status_code=410,
            detail="Conversion result is no longer available",
        )

    result_path = Path(job.resultPath)
    if result_path.suffix == ".ezs":
        media_type = "application/json"
    else:
        media_type = "application/vnd.recordare.musicxml+xml"

    return FileResponse(
        job.resultPath,
        media_type=media_type,
        filename=result_path.name,
    )
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "notestream-conversion-api",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
