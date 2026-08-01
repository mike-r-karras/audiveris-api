import asyncio
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

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
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

        job = jobs[job_id]
        job.resultPath = str(result_path)

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
    job_id = str(uuid.uuid4())
    work_dir = Path(tempfile.mkdtemp(prefix=f"conversion-{job_id}-"))
    input_path = work_dir / (file.filename or "score.pdf")
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

    asyncio.create_task(
        run_audiveris(
            job_id=job_id,
            input_path=input_path,
            output_dir=output_dir,
        )
    )

    return {
        "jobId": job_id,
        "status": "queued",
        "progressUrl": f"/conversions/{job_id}",
        "resultUrl": f"/conversions/{job_id}/result",
    }


@app.get("/conversions/{job_id}", response_model=ConversionJob)
async def get_conversion(job_id: str):
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

    return FileResponse(
        job.resultPath,
        media_type="application/vnd.recordare.musicxml+xml",
        filename=Path(job.resultPath).name,
    )
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "notestream-conversion-api",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }