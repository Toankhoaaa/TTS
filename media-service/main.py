"""
Media Service API Server

Endpoints:
- GET  /              - Serve UI
- POST /upload        - Upload video
- POST /process/<id>  - Process video
- GET  /status/<id>   - Get job status
- POST /download-url  - Download video from URL to server (returns job_id)
- GET  /download/<id> - Download video file to client (returns .mp4)
- GET  /watch/<id>    - Watch video with player UI
"""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi import BackgroundTasks
from pydantic import BaseModel

from app.config import settings
from app.pipeline import PipelineProcessor, PipelineConfig, PipelineStep
from app.download import DownloadService, get_download_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="TTS Video Processor API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Thread pool for heavy processing
executor = ThreadPoolExecutor(max_workers=settings.max_concurrent_jobs)

# In-memory job storage (in production, use Redis or database)
jobs = {}

# Download service instance
download_service: Optional[DownloadService] = None


def get_dl_service() -> DownloadService:
    """Get or create download service."""
    global download_service
    if download_service is None:
        venv_path = Path(__file__).parent / "venv"
        download_service = DownloadService(
            output_dir=settings.videos_dir,
            venv_path=venv_path,
            cookies_file=None,
        )
    return download_service


class ProcessRequest(BaseModel):
    source_lang: Optional[str] = "auto"
    target_lang: str = "vi"
    voice: str = "vi-VN-HoaiMyNeural"
    add_subtitles: bool = True
    add_voiceover: bool = True
    voiceover_volume: float = 0.8


class JobStatus(BaseModel):
    job_id: str
    status: str
    video_path: Optional[str] = None
    error: Optional[str] = None
    progress: int = 0


@app.get("/")
async def serve_ui():
    """Serve the UI."""
    ui_path = Path(__file__).parent / "ui" / "index.html"
    if ui_path.exists():
        return FileResponse(str(ui_path))
    raise HTTPException(status_code=404, detail="UI not found")


@app.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    """Upload a video file."""
    import uuid
    
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    
    # Generate job ID
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    job_dir = settings.media_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    
    # Save uploaded file
    video_path = job_dir / "input.mp4"
    
    try:
        with open(video_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")
    
    # Store job info
    jobs[job_id] = {
        "status": "uploaded",
        "video_path": str(video_path),
        "job_dir": str(job_dir),
        "progress": 10
    }
    
    logger.info(f"Video uploaded: {job_id}")
    
    return {
        "job_id": job_id,
        "status": "uploaded",
        "progress": 10
    }


@app.post("/upload-vietsub/{job_id}")
async def upload_vietsub(job_id: str, file: UploadFile = File(...)):
    """
    Upload Vietnamese subtitle (vietsub) SRT file for a job.
    This replaces the STT step - TTS will use this subtitle directly.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if not file.filename or not file.filename.endswith('.srt'):
        raise HTTPException(status_code=400, detail="Only SRT files are supported")
    
    job = jobs[job_id]
    job_dir = Path(job["job_dir"])
    
    # Save vietsub file
    vietsub_path = job_dir / "vietsub.srt"
    
    try:
        content = await file.read()
        vietsub_path.write_bytes(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save vietsub: {str(e)}")
    
    # Validate SRT format
    try:
        from app.utils import parse_srt_file
        segments = parse_srt_file(vietsub_path)
        if not segments:
            raise HTTPException(status_code=400, detail="SRT file is empty or invalid")
        segment_count = len(segments)
    except Exception as e:
        vietsub_path.unlink()
        raise HTTPException(status_code=400, detail=f"Invalid SRT format: {str(e)}")
    
    job["vietsub_path"] = str(vietsub_path)
    logger.info(f"Vietsub uploaded for {job_id}: {segment_count} segments")
    
    return {
        "job_id": job_id,
        "status": "vietsub_uploaded",
        "segments": segment_count
    }


@app.post("/process/{job_id}")
async def process_video(job_id: str, request: ProcessRequest):
    """Process a video with transcription, translation, TTS, and rendering."""
    
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job = jobs[job_id]
    video_path = Path(job["video_path"])
    
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")
    
    job["status"] = "processing"
    job["progress"] = 20
    
    try:
        # Configure pipeline
        logger.info(f"[DEBUG] ProcessRequest: voice={request.voice}, add_subtitles={request.add_subtitles}, add_voiceover={request.add_voiceover}, voiceover_volume={request.voiceover_volume}")
        config = PipelineConfig(
            source_lang=request.source_lang if request.source_lang != "auto" else None,
            target_lang=request.target_lang,
            voice=request.voice,
            add_subtitles=request.add_subtitles,
            add_voiceover=request.add_voiceover,
            voiceover_volume=request.voiceover_volume
        )
        logger.info(f"[DEBUG] PipelineConfig: voice={config.voice}, add_subtitles={config.add_subtitles}, add_voiceover={config.add_voiceover}, voiceover_volume={config.voiceover_volume}")
        
        processor = PipelineProcessor(config)
        
        # Check if vietsub was uploaded
        vietsub_path = job.get("vietsub_path")
        
        # Process asynchronously
        loop = asyncio.get_event_loop()
        
        if vietsub_path and Path(vietsub_path).exists():
            # Use vietsub - skip STT and translation
            logger.info(f"Processing with vietsub: {vietsub_path}")
            result = await loop.run_in_executor(
                executor,
                processor.process_with_vietsub,
                video_path,
                Path(vietsub_path),
                Path(job["job_dir"]),
                job_id
            )
        else:
            # Normal processing with STT + translation
            result = await loop.run_in_executor(
                executor,
                processor.process,
                video_path,
                Path(job["job_dir"]),
                job_id
            )
        
        if result.success:
            job["status"] = "completed"
            job["progress"] = 100
            job["output_path"] = str(result.output_path)
            job["subtitle_path"] = str(result.subtitle_path)
            job["translated_path"] = str(result.translated_path)

            logger.info(f"Processing completed: {job_id}")
            
            return {
                "job_id": job_id,
                "status": "completed",
                "progress": 100,
                "output_path": str(result.output_path)
            }
        else:
            job["status"] = "failed"
            job["error"] = result.error
            
            logger.error(f"Processing failed: {job_id} - {result.error}")
            
            return {
                "job_id": job_id,
                "status": "failed",
                "error": result.error,
                "progress": job["progress"]
            }
            
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        logger.error(f"Processing error: {job_id} - {e}")
        
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    """Get job status."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job = jobs[job_id]
    
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job.get("progress", 0),
        "output_path": job.get("output_path"),
        "error": job.get("error")
    }


@app.get("/download-original/{job_id}")
async def download_original_video(job_id: str):
    """
    Download original video - tìm trong videos/ trước, rồi mới đến media_dir/
    """
    # 1. Thử trong videos/ (file từ download-url)
    video_path = settings.videos_dir / f"{job_id}.mp4"
    
    # 2. Thử trong media_dir/{job_id}/ (file từ upload)
    if not video_path.exists():
        for fname in ["input.mp4", "video.mp4", "output.mp4"]:
            alt_path = settings.media_dir / job_id / fname
            if alt_path.exists():
                video_path = alt_path
                break
    
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    
    # Verify file size > 0
    if video_path.stat().st_size == 0:
        raise HTTPException(status_code=500, detail="Video file is corrupted (empty)")
    
    # Use FileResponse for proper Content-Disposition header
    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"video_{job_id}.mp4"
    )


@app.get("/download/{job_id}")
async def download_video(job_id: str):
    """
    Trả file video đã xử lý về client (download).
    
    Thứ tự tìm kiếm:
    1. data/{job_id}/final_*.mp4 (video đã xử lý - upload)
    2. data/{job_id}/video_*.mp4 (video đã xử lý - alternative)
    3. videos/final_{job_id}.mp4 (video đã xử lý - download URL)
    4. videos/{job_id}.mp4 (video gốc từ download-url)
    5. data/{job_id}/input.mp4 (video gốc từ upload)
    """
    
    # 1. Thử video đã xử lý trong data/ (từ upload/process)
    data_dir = settings.media_dir / job_id
    if data_dir.exists():
        # Tìm file final_*.mp4
        for f in data_dir.glob("final_*.mp4"):
            if f.stat().st_size > 0:
                logger.info(f"Serving processed video: {f}")
                return FileResponse(
                    path=str(f),
                    media_type="video/mp4",
                    filename=f"video_{job_id}_processed.mp4"
                )
        # Tìm file video_*.mp4 (alternative naming)
        for f in data_dir.glob("video_*.mp4"):
            if f.stat().st_size > 0:
                logger.info(f"Serving processed video: {f}")
                return FileResponse(
                    path=str(f),
                    media_type="video/mp4",
                    filename=f"video_{job_id}_processed.mp4"
                )
    
    # 2. Thử video đã xử lý trong videos/ (từ download-url)
    processed_path = settings.videos_dir / f"final_{job_id}.mp4"
    if processed_path.exists() and processed_path.stat().st_size > 0:
        logger.info(f"Serving processed video: {processed_path}")
        return FileResponse(
            path=str(processed_path),
            media_type="video/mp4",
            filename=f"video_{job_id}_processed.mp4"
        )
    
    # 3. Thử video gốc trong videos/ (từ download-url)
    video_path = settings.videos_dir / f"{job_id}.mp4"
    
    # 4. Thử video gốc trong data/{job_id}/
    if not video_path.exists():
        for fname in ["input.mp4", "video.mp4", "output.mp4"]:
            alt_path = data_dir / fname
            if alt_path.exists():
                video_path = alt_path
                break
    
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    
    # Verify file size > 0
    if video_path.stat().st_size == 0:
        raise HTTPException(status_code=500, detail="Video file is corrupted (empty)")
    
    logger.info(f"Serving original video: {video_path}")
    
    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"video_{job_id}.mp4"
    )


@app.get("/preview/{job_id}")
async def preview_video(job_id: str):
    """Preview video with subtitles burned in (from existing files)."""
    from app.render import RenderService
    
    job_dir = settings.media_dir / job_id
    input_path = job_dir / "input.mp4"
    
    if not input_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    
    # Find subtitle file
    subtitle_files = list(job_dir.glob("*.srt"))
    subtitle_path = None
    for sf in subtitle_files:
        if "translated" in sf.name:
            subtitle_path = sf
            break
    if not subtitle_path and subtitle_files:
        subtitle_path = subtitle_files[0]
    
    output_path = job_dir / "preview.mp4"
    
    # Render video with subtitles
    render = RenderService()
    result = render.render_video(
        video_path=input_path,
        subtitle_path=subtitle_path,
        audio_path=None,
        output_path=output_path,
        subtitle_track=True
    )
    
    if result.success:
        return FileResponse(
            path=str(output_path),
            media_type="video/mp4"
        )
    else:
        raise HTTPException(status_code=500, detail=f"Render failed: {result.error}")


@app.get("/watch/{job_id}")
async def watch_video(job_id: str):
    """Watch video with full video player UI."""
    from app.render import RenderService
    
    job_dir = settings.media_dir / job_id
    input_path = job_dir / "input.mp4"
    
    if not input_path.exists():
        raise HTMLResponse(
            "<html><body><h1>Video not found</h1><p>Job ID: " + job_id + "</p></body></html>",
            status_code=404
        )
    
    # Find subtitle file
    subtitle_files = list(job_dir.glob("*.srt"))
    subtitle_path = None
    for sf in subtitle_files:
        if "translated" in sf.name:
            subtitle_path = sf
            break
    if not subtitle_path and subtitle_files:
        subtitle_path = subtitle_files[0]
    
    output_path = job_dir / "preview.mp4"
    
    # Render video with subtitles if not already done
    if not output_path.exists() and subtitle_path:
        render = RenderService()
        result = render.render_video(
            video_path=input_path,
            subtitle_path=subtitle_path,
            audio_path=None,
            output_path=output_path,
            subtitle_track=True
        )
        if not result.success:
            return HTMLResponse(
                "<html><body><h1>Render failed</h1><p>" + str(result.error) + "</p></body></html>",
                status_code=500
            )
    
    # Return HTML video player
    video_url = f"/preview/{job_id}"
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Video Player - {job_id}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            background: #000; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            min-height: 100vh;
            font-family: Arial, sans-serif;
        }}
        .video-container {{
            width: 100%;
            max-width: 1280px;
            position: relative;
        }}
        video {{
            width: 100%;
            max-height: 90vh;
        }}
        .back-btn {{
            position: fixed;
            top: 20px;
            left: 20px;
            padding: 10px 20px;
            background: #333;
            color: white;
            text-decoration: none;
            border-radius: 5px;
            font-size: 14px;
        }}
        .back-btn:hover {{ background: #555; }}
    </style>
</head>
<body>
    <a href="/" class="back-btn">← Back to Upload</a>
    <div class="video-container">
        <video controls autoplay>
            <source src="{video_url}" type="video/mp4">
            Your browser does not support the video tag.
        </video>
    </div>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/subtitles/{job_id}")
async def get_subtitles(job_id: str):
    """
    Get translated subtitles for a job.
    Returns JSON with segments: text, start time, end time.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    # Use translated_path (translated) instead of subtitle_path (original STT)
    subtitle_path = job.get("translated_path") or job.get("subtitle_path")

    if not subtitle_path or not Path(subtitle_path).exists():
        raise HTTPException(status_code=404, detail="Subtitle file not found")

    from app.utils import parse_srt_file
    segments = parse_srt_file(Path(subtitle_path))

    return {
        "job_id": job_id,
        "segments": [
            {
                "index": seg.index,
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "start_str": format_time(seg.start),
                "end_str": format_time(seg.end)
            }
            for seg in segments
        ]
    }


def format_time(seconds: float) -> str:
    """Format seconds to SRT timestamp HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


@app.get("/subtitles-compare/{job_id}")
async def get_subtitles_compare(job_id: str):
    """
    Get both original and translated subtitles for comparison.
    Returns JSON with segments showing original text, translated text, and timing.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]

    # Get original (STT) subtitles
    original_path = job.get("subtitle_path")
    # Get translated subtitles
    translated_path = job.get("translated_path") or original_path

    from app.utils import parse_srt_file

    original_segments = []
    if original_path and Path(original_path).exists():
        original_segments = parse_srt_file(Path(original_path))

    translated_segments = []
    if translated_path and Path(translated_path).exists():
        translated_segments = parse_srt_file(Path(translated_path))

    # Build segments list - match by index
    segments = []
    for i, orig_seg in enumerate(original_segments):
        trans_seg = translated_segments[i] if i < len(translated_segments) else None
        segments.append({
            "index": orig_seg.index,
            "start": orig_seg.start,
            "end": orig_seg.end,
            "start_str": format_time(orig_seg.start),
            "end_str": format_time(orig_seg.end),
            "original": orig_seg.text,
            "translated": trans_seg.text if trans_seg else orig_seg.text
        })

    return {
        "job_id": job_id,
        "segments": segments
    }


@app.get("/subtitle/{job_id}")
async def download_subtitle(job_id: str):
    """Download subtitle file."""

    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    subtitle_path = job.get("subtitle_path")

    if not subtitle_path or not Path(subtitle_path).exists():
        raise HTTPException(status_code=404, detail="Subtitle file not found")

    return FileResponse(
        path=subtitle_path,
        filename=f"subtitle_{job_id}.srt",
        media_type="text/vtt"
    )


@app.post("/download-url")
async def download_from_url(request: dict):
    """
    Download video từ URL về server.
    Sử dụng multi-strategy downloader với 3-layer validation.
    
    Input: { "url": "..." }
    Output: { "job_id": "...", "file_size": ..., "status": "downloaded" }
    """
    url = request.get("url", "")
    
    # Validate URL
    if not url or len(url) < 5:
        raise HTTPException(status_code=400, detail="Invalid URL")
    
    # Check URL format
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    
    try:
        # Use download service
        dl_service = get_dl_service()
        result = dl_service.download(url)
        
        if not result.success:
            raise HTTPException(
                status_code=500,
                detail=f"Download failed: {result.error}"
            )
        
        # Sync with main jobs dict
        jobs[result.job_id] = {
            "status": "downloaded",
            "video_path": result.file_path,
            "job_dir": str(settings.videos_dir),
            "progress": 5,
            "file_size": result.file_size,
            "source": "url",
            "platform": result.platform,
            "strategy": result.strategy_used,
        }
        
        logger.info(
            f"Download successful: {result.job_id} "
            f"({result.human_readable_size}) "
            f"via {result.strategy_used} "
            f"from {result.platform}"
        )
        
        return {
            "job_id": result.job_id,
            "status": "downloaded",
            "progress": 5,
            "file_size": result.file_size,
            "platform": result.platform,
            "message": "Download completed and verified"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=True)
