import os
import uuid
import logging
from pathlib import Path
from typing import Optional, Tuple, List
from datetime import datetime

from app.config import settings

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def generate_job_id() -> str:
    """Generate unique job ID."""
    return f"job_{uuid.uuid4().hex[:12]}"


def generate_task_id() -> str:
    """Generate unique task ID."""
    return f"task_{uuid.uuid4().hex[:12]}"


def get_media_path(job_id: str, filename: str) -> Path:
    """Get full path for media file."""
    job_dir = settings.media_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir / filename


def get_temp_path(filename: str) -> Path:
    """Get full path for temp file."""
    return settings.temp_dir / filename


def format_timestamp(seconds: float) -> str:
    """Format seconds to SRT timestamp format (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_timestamp(timestamp: str) -> float:
    """Parse SRT timestamp to seconds."""
    try:
        parts = timestamp.replace(',', ':').split(':')
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])
        millis = int(parts[3])
        return hours * 3600 + minutes * 60 + seconds + millis / 1000
    except Exception as e:
        logger.warning(f"Failed to parse timestamp {timestamp}: {e}")
        return 0.0


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_temp_files(*paths: Path) -> None:
    """Clean up temporary files."""
    for path in paths:
        try:
            if path.exists():
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    import shutil
                    shutil.rmtree(path)
                logger.debug(f"Cleaned up: {path}")
        except Exception as e:
            logger.warning(f"Failed to cleanup {path}: {e}")


def get_file_size_mb(path: Path) -> float:
    """Get file size in MB."""
    if path.exists():
        return path.stat().st_size / (1024 * 1024)
    return 0.0


def sanitize_text(text: str) -> str:
    """Sanitize text - remove only control characters, preserve all Unicode."""
    if not text:
        return text
    import re
    # Remove only control characters (ASCII 0-31 except tab/newline, and DEL)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    return text


def detect_language_code(lang: str) -> str:
    """Map language code to proper format."""
    lang_map = {
        'zh': 'zh-CN',
        'tw': 'zh-TW',
        'cn': 'zh-CN',
        'en': 'en-US',
        'vi': 'vi-VN',
        'ja': 'ja-JP',
        'ko': 'ko-KR',
        'th': 'th-TH',
        'id': 'id-ID',
        'ms': 'ms-MY',
    }
    return lang_map.get(lang.lower(), lang)


def get_tts_voice_for_language(lang: str) -> str:
    """Get appropriate TTS voice for language."""
    voice_map = {
        'vi': 'vi-VN-HoaiMyNeural',
        'en': 'en-US-AriaNeural',
        'zh': 'zh-CN-XiaoxiaoNeural',
        'ja': 'ja-JP-NanamiNeural',
        'ko': 'ko-KR-SunhiNeural',
        'th': 'th-TH-PremwadeeNeural',
        'id': 'id-ID-GadisNeural',
        'ms': 'ms-MY-YasminNeural',
        'fr': 'fr-FR-DeniseNeural',
        'de': 'de-DE-KatjaNeural',
        'es': 'es-ES-ElviraNeural',
        'pt': 'pt-BR-FranciscaNeural',
        'ru': 'ru-RU-DariyaNeural',
        'ar': 'ar-SA-ZariyahNeural',
    }
    lang_base = lang.split('-')[0].lower()
    return voice_map.get(lang_base, 'vi-VN-HoaiMyNeural')


def parse_srt_file(srt_path: Path) -> List['SubtitleSegment']:
    """Parse SRT file and return list of SubtitleSegment dataclass."""
    from dataclasses import dataclass
    
    @dataclass
    class SubtitleSegment:
        index: int
        start: float
        end: float
        text: str
    
    if not srt_path.exists():
        raise FileNotFoundError(f"SRT file not found: {srt_path}")
    
    content = srt_path.read_text(encoding="utf-8")
    segments = []
    
    # Split by double newlines
    blocks = content.strip().split("\n\n")
    
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        
        try:
            # First line is index
            index = int(lines[0].strip())
            
            # Second line is timestamp
            timestamp = lines[1].strip()
            start_str, end_str = timestamp.split(" --> ")
            start = parse_timestamp(start_str)
            end = parse_timestamp(end_str)
            
            # Rest is text
            text = "\n".join(lines[2:])
            
            segments.append(SubtitleSegment(
                index=index,
                start=start,
                end=end,
                text=text
            ))
        except (ValueError, IndexError) as e:
            logger.warning(f"Failed to parse SRT block: {e}")
            continue
    
    return segments


class TimingInfo:
    """Container for timing information."""

    def __init__(self, start: float, end: float, text: str = ""):
        self.start = start
        self.end = end
        self.text = text
        self.duration = end - start

    def __repr__(self):
        return f"TimingInfo(start={self.start:.2f}, end={self.end:.2f}, text='{self.text[:30]}...')"
