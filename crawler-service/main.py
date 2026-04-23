"""
Crawler Service - Video download from various sources.

Supports:
- TikTok
- Douyin
- Local files
- Direct URLs
"""

import os
import uuid
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/app/data"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Crawler Service", version="1.0.0")


class DownloadRequest(BaseModel):
    """Request to download video from URL."""
    url: str
    job_id: Optional[str] = None


class DownloadResponse(BaseModel):
    """Response with download result."""
    success: bool
    video_path: Optional[str] = None
    filename: Optional[str] = None
    job_id: str
    error: Optional[str] = None


def is_tiktok_url(url: str) -> bool:
    """Check if URL is from TikTok."""
    return 'tiktok.com' in url.lower()


def is_douyin_url(url: str) -> bool:
    """Check if URL is from Douyin."""
    return 'douyin.com' in url.lower() or 'iesdouyin.com' in url.lower()


def is_supported_url(url: str) -> bool:
    """Check if URL is a supported platform."""
    supported = [
        'tiktok.com',
        'douyin.com',
        'iesdouyin.com',
        'youtube.com',
        'youtu.be',
        'instagram.com',
        'twitter.com',
        'x.com',
    ]
    return any(platform in url.lower() for platform in supported)


async def download_with_ytdlp(url: str, output_dir: Path, job_id: str) -> tuple[bool, Optional[Path], Optional[str]]:
    """
    Download video using yt-dlp.

    Returns:
        Tuple of (success, video_path, error_message)
    """
    output_template = str(output_dir / "%(title)s_%(id)s.%(ext)s")

    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'outtmpl': output_template,
        'quiet': False,
        'no_warnings': False,
        'extract_flat': False,
    }

    # TikTok specific options
    if is_tiktok_url(url):
        ydl_opts.update({
            'extractor_args': {
                'tiktok': {
                    'download': 'video',
                }
            },
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }
        })

    try:
        logger.info(f"Downloading from: {url}")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            if info is None:
                return False, None, "Failed to extract video info"

            # Find downloaded file
            filename = ydl.prepare_filename(info)

            # Handle multiple formats
            if not Path(filename).exists():
                for ext in ['mp4', 'mkv', 'webm', 'flv']:
                    alt_filename = filename.rsplit('.', 1)[0] + f'.{ext}'
                    if Path(alt_filename).exists():
                        filename = alt_filename
                        break

            video_path = Path(filename)

            if video_path.exists():
                # Move to job directory if needed
                job_dir = output_dir / job_id
                job_dir.mkdir(parents=True, exist_ok=True)

                if video_path.parent != job_dir:
                    new_path = job_dir / video_path.name
                    video_path.rename(new_path)
                    video_path = new_path

                logger.info(f"Downloaded: {video_path}")
                return True, video_path, None
            else:
                return False, None, "Downloaded file not found"

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download error: {e}")
        return False, None, f"Download failed: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return False, None, f"Unexpected error: {str(e)}"


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "crawler"}


@app.post("/download", response_model=DownloadResponse)
async def download_video(request: DownloadRequest):
    """
    Download video from URL.

    Supports TikTok, Douyin, YouTube, Instagram, Twitter, and other platforms.
    """
    job_id = request.job_id or str(uuid.uuid4())[:12]

    if not request.url:
        raise HTTPException(status_code=400, detail="URL is required")

    # Validate URL
    try:
        parsed = urlparse(request.url)
        if not parsed.scheme:
            # Try adding https://
            request.url = "https://" + request.url
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

    # Check if supported
    if not is_supported_url(request.url) and not request.url.startswith('http'):
        raise HTTPException(status_code=400, detail="Unsupported URL format")

    output_dir = MEDIA_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # Download
    success, video_path, error = await download_with_ytdlp(request.url, output_dir, job_id)

    if success and video_path:
        return DownloadResponse(
            success=True,
            video_path=str(video_path),
            filename=video_path.name,
            job_id=job_id
        )
    else:
        return DownloadResponse(
            success=False,
            job_id=job_id,
            error=error
        )


@app.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    """
    Upload local video file.

    Returns job_id and file path.
    """
    job_id = str(uuid.uuid4())[:12]
    job_dir = MEDIA_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    file_path = job_dir / file.filename

    try:
        content = await file.read()
        file_path.write_bytes(content)

        logger.info(f"Uploaded file: {file_path}")

        return DownloadResponse(
            success=True,
            video_path=str(file_path),
            filename=file.filename,
            job_id=job_id
        )
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@app.get("/file/{job_id}/{filename}")
async def get_file(job_id: str, filename: str):
    """Get downloaded file."""
    file_path = MEDIA_DIR / job_id / filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=file_path,
        filename=filename,
        media_type='video/mp4'
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
