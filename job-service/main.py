"""
Job Service - Orchestrates video processing jobs.

Coordinates between:
- Crawler Service (video download)
- Media Service (processing)
"""

import os
import uuid
import logging
from enum import Enum
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/app/data"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

MEDIA_SERVICE_URL = os.getenv("MEDIA_SERVICE_URL", "http://localhost:8001")
CRAWLER_SERVICE_URL = os.getenv("CRAWLER_SERVICE_URL", "http://localhost:8002")

app = FastAPI(title="Job Service", version="1.0.0")

# In-memory job storage (use Redis/DB in production)
jobs: Dict[str, Dict[str, Any]] = {}


class JobStatus(str, Enum):
    """Job status enumeration."""
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ProcessOptions(BaseModel):
    """Options for video processing."""
    add_subtitles: bool = True
    add_voiceover: bool = True
    source_lang: Optional[str] = None
    voice: Optional[str] = None


class CreateJobRequest(BaseModel):
    """Request to create a new job."""
    url: Optional[str] = None
    video_path: Optional[str] = None
    job_id: Optional[str] = None
    options: ProcessOptions = ProcessOptions()


class JobResponse(BaseModel):
    """Job information response."""
    job_id: str
    status: JobStatus
    video_path: Optional[str] = None
    subtitle_path: Optional[str] = None
    audio_path: Optional[str] = None
    output_path: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str


def update_job(job_id: str, **kwargs) -> None:
    """Update job information."""
    if job_id in jobs:
        jobs[job_id].update(kwargs)
        jobs[job_id]['updated_at'] = datetime.now().isoformat()


async def process_video_task(
    job_id: str,
    video_path: str,
    options: ProcessOptions
) -> None:
    """
    Background task to process video.

    Pipeline:
    1. Extract audio
    2. Transcribe
    3. Translate
    4. Generate TTS
    5. Render final video
    """
    logger.info(f"Starting processing for job: {job_id}")

    try:
        # Step 1: Transcribe
        update_job(job_id, status=JobStatus.PROCESSING, step="transcribing")

        async with httpx.AsyncClient(timeout=300.0) as client:
            # Transcribe
            with open(video_path, 'rb') as f:
                files = {'file': ('video.mp4', f, 'video/mp4')}
                data = {'job_id': job_id}

                if options.source_lang:
                    data['language'] = options.source_lang

                resp = await client.post(
                    f"{MEDIA_SERVICE_URL}/transcribe",
                    files=files,
                    data=data
                )

            if resp.status_code != 200:
                raise Exception(f"Transcription failed: {resp.text}")

            transcribe_result = resp.json()
            subtitle_path = transcribe_result.get('subtitle_path')

            # Step 2: Translate
            update_job(job_id, step="translating")

            resp = await client.post(
                f"{MEDIA_SERVICE_URL}/translate",
                json={
                    'subtitle_path': subtitle_path,
                    'source_lang': options.source_lang or 'auto'
                }
            )

            if resp.status_code != 200:
                raise Exception(f"Translation failed: {resp.text}")

            translate_result = resp.json()
            translated_path = translate_result.get('translated_path')

            # Step 3: Generate TTS
            update_job(job_id, step="generating_tts")

            resp = await client.post(
                f"{MEDIA_SERVICE_URL}/tts",
                json={
                    'subtitle_path': translated_path,
                    'voice': options.voice
                }
            )

            if resp.status_code != 200:
                raise Exception(f"TTS generation failed: {resp.text}")

            tts_result = resp.json()
            voiceover_path = tts_result.get('voiceover_path')

            # Step 4: Render
            update_job(job_id, step="rendering")

            resp = await client.post(
                f"{MEDIA_SERVICE_URL}/render",
                json={
                    'video_path': video_path,
                    'subtitle_path': translated_path,
                    'voiceover_path': voiceover_path,
                    'job_id': job_id
                }
            )

            if resp.status_code != 200:
                raise Exception(f"Rendering failed: {resp.text}")

            render_result = resp.json()

            # Success
            update_job(
                job_id,
                status=JobStatus.COMPLETED,
                output_path=render_result.get('output_path'),
                subtitle_path=translated_path,
                audio_path=voiceover_path,
                step="completed"
            )

            logger.info(f"Job {job_id} completed successfully")

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}")
        update_job(job_id, status=JobStatus.FAILED, error=str(e))


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "job"}


@app.post("/jobs", response_model=JobResponse)
async def create_job(request: CreateJobRequest, background_tasks: BackgroundTasks):
    """
    Create a new processing job.

    Can accept either:
    - URL to download video
    - Path to local video file
    """
    job_id = request.job_id or str(uuid.uuid4())[:12]

    # Validate input
    if not request.url and not request.video_path:
        raise HTTPException(
            status_code=400,
            detail="Either 'url' or 'video_path' is required"
        )

    # Create job entry
    jobs[job_id] = {
        'job_id': job_id,
        'status': JobStatus.PENDING,
        'video_path': None,
        'subtitle_path': None,
        'audio_path': None,
        'output_path': None,
        'error': None,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
        'step': 'initializing'
    }

    video_path = None

    # Download or validate video
    if request.url:
        update_job(job_id, status=JobStatus.DOWNLOADING, step="downloading")

        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{CRAWLER_SERVICE_URL}/download",
                json={'url': request.url, 'job_id': job_id}
            )

            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Download failed: {resp.text}"
                )

            result = resp.json()

            if not result.get('success'):
                raise HTTPException(
                    status_code=502,
                    detail=f"Download failed: {result.get('error')}"
                )

            video_path = result['video_path']

    elif request.video_path:
        video_path = request.video_path

    # Validate video exists
    if not video_path or not Path(video_path).exists():
        raise HTTPException(status_code=400, detail="Video file not found")

    # Update job with video path
    update_job(job_id, video_path=video_path)

    # Start processing
    background_tasks.add_task(
        process_video_task,
        job_id,
        video_path,
        request.options
    )

    return JobResponse(**jobs[job_id])


@app.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    """Get job information."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobResponse(**jobs[job_id])


@app.get("/jobs/{job_id}/result")
async def get_job_result(job_id: str):
    """Get job result (output video)."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]

    if job['status'] != JobStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"Job not completed. Status: {job['status']}"
        )

    output_path = job.get('output_path')

    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=404, detail="Output file not found")

    return FileResponse(
        path=output_path,
        filename=Path(output_path).name,
        media_type='video/mp4'
    )


@app.get("/jobs/{job_id}/subtitles")
async def get_job_subtitles(job_id: str):
    """Get subtitle file."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    subtitle_path = job.get('subtitle_path')

    if not subtitle_path or not Path(subtitle_path).exists():
        raise HTTPException(status_code=404, detail="Subtitle file not found")

    return FileResponse(
        path=subtitle_path,
        filename=Path(subtitle_path).name,
        media_type='text/vtt'
    )


@app.get("/jobs")
async def list_jobs(status: Optional[JobStatus] = None, limit: int = 50):
    """List all jobs."""
    result = list(jobs.values())

    if status:
        result = [j for j in result if j['status'] == status]

    # Sort by created_at descending
    result.sort(key=lambda x: x['created_at'], reverse=True)

    return result[:limit]


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Delete a job and its files."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    # Clean up files
    job = jobs[job_id]
    for key in ['video_path', 'output_path', 'subtitle_path', 'audio_path']:
        path = job.get(key)
        if path and Path(path).exists():
            try:
                Path(path).unlink()
            except Exception as e:
                logger.warning(f"Failed to delete {path}: {e}")

    # Remove job
    del jobs[job_id]

    return {"message": f"Job {job_id} deleted"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
