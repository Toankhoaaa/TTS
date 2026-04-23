"""
Video Validator - 3-Layer Validation for Downloaded Files

Ensures downloaded files are REAL video files, not HTML error pages or stubs.
"""

import subprocess
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List
from enum import Enum

logger = logging.getLogger(__name__)


class ValidationLevel(Enum):
    """Validation strictness levels."""
    MINIMAL = "minimal"      # Size check only
    STANDARD = "standard"    # Size + content
    STRICT = "strict"        # Size + content + ffprobe


@dataclass
class ValidationResult:
    """Result of video validation."""
    is_valid: bool
    file_size: int
    file_path: str
    errors: List[str]
    warnings: List[str]
    
    @property
    def human_readable_size(self) -> str:
        """Convert file size to human readable format."""
        size = self.file_size
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} TB"


class VideoValidator:
    """
    3-Layer Video Validator.
    
    Layer 1: File size check (reject files < 50KB - too small to be real video)
    Layer 2: Content check (reject HTML pages)
    Layer 3: FFprobe validation (verify video codec)
    """
    
    # Minimum file size for a valid video (50KB - allows short clips like TikTok)
    MIN_FILE_SIZE = 50_000  # 50KB
    
    # FFprobe executable name
    FFPROBE_NAME = "ffprobe.exe"
    
    def __init__(self, venv_path: Optional[Path] = None, level: ValidationLevel = ValidationLevel.STRICT):
        """
        Initialize validator.
        
        Args:
            venv_path: Path to virtual environment (for ffprobe)
            level: Validation strictness level
        """
        self.venv_path = venv_path
        self.level = level
        
        # Find ffprobe
        self.ffprobe_path = self._find_ffprobe()
    
    def _find_ffprobe(self) -> Optional[Path]:
        """Find ffprobe executable."""
        if not self.venv_path:
            return None
            
        ffprobe = self.venv_path / "Scripts" / self.FFPROBE_NAME
        if ffprobe.exists():
            logger.info(f"Found ffprobe at: {ffprobe}")
            return ffprobe
        
        # Try common locations
        common_paths = [
            Path("C:/ffmpeg/bin/ffprobe.exe"),
            Path("C:/Program Files/ffmpeg/bin/ffprobe.exe"),
            Path("ffmpeg/bin/ffprobe.exe"),
        ]
        for path in common_paths:
            if path.exists():
                logger.info(f"Found ffprobe at: {path}")
                return path
        
        logger.warning("ffprobe not found - skipping codec validation")
        return None
    
    def validate(self, file_path: Path) -> ValidationResult:
        """
        Validate a downloaded video file using 3 layers.
        
        Args:
            file_path: Path to the file to validate
            
        Returns:
            ValidationResult with validation status and details
        """
        errors = []
        warnings = []
        
        # Layer 0: Check file exists
        if not file_path.exists():
            return ValidationResult(
                is_valid=False,
                file_size=0,
                file_path=str(file_path),
                errors=["File does not exist"],
                warnings=[]
            )
        
        # Get file size
        try:
            file_size = file_path.stat().st_size
        except OSError as e:
            return ValidationResult(
                is_valid=False,
                file_size=0,
                file_path=str(file_path),
                errors=[f"Cannot get file size: {e}"],
                warnings=[]
            )
        
        # Layer 1: Size validation
        size_valid, size_error = self._validate_size(file_size)
        if not size_valid:
            errors.append(size_error)
        
        # Layer 2: Content validation
        if file_size > 0:
            content_valid, content_error = self._validate_content(file_path)
            if not content_valid:
                errors.append(content_error)
        else:
            warnings.append("File is empty")
        
        # Layer 3: FFprobe validation (only if size and content are OK)
        if not errors and self.level != ValidationLevel.MINIMAL:
            ffprobe_valid, ffprobe_error = self._validate_with_ffprobe(file_path)
            if not ffprobe_valid:
                if self.level == ValidationLevel.STRICT:
                    errors.append(ffprobe_error)
                else:
                    warnings.append(ffprobe_error)
        
        is_valid = len(errors) == 0
        
        result = ValidationResult(
            is_valid=is_valid,
            file_size=file_size,
            file_path=str(file_path),
            errors=errors,
            warnings=warnings
        )
        
        # Log validation result
        if is_valid:
            logger.info(
                f"✓ Validation passed: {file_path.name} "
                f"({result.human_readable_size})"
            )
        else:
            logger.error(
                f"✗ Validation failed: {file_path.name} "
                f"({result.human_readable_size}) - {'; '.join(errors)}"
            )
        
        return result
    
    def _validate_size(self, file_size: int) -> tuple[bool, str]:
        """
        Layer 1: Validate file size.
        
        A real video should be at least 1MB.
        """
        if file_size < self.MIN_FILE_SIZE:
            return False, f"File too small ({file_size:,} bytes < {self.MIN_FILE_SIZE:,} bytes minimum)"
        return True, ""
    
    def _validate_content(self, file_path: Path) -> tuple[bool, str]:
        """
        Layer 2: Validate file content.
        
        Check if the file is actually a video or an HTML error page.
        """
        try:
            with open(file_path, 'rb') as f:
                header = f.read(2000)  # Read first 2KB
            
            # Check for HTML markers
            header_text = header.lower()
            
            html_markers = [
                (b'<html', "HTML tag found"),
                (b'<!doctype', "DOCTYPE declaration found"),
                (b'<!DOCTYPE', "DOCTYPE declaration found"),
                (b'<head', "HEAD tag found"),
                (b'<body', "BODY tag found"),
                (b'<!doctype html>', "HTML5 doctype found"),
            ]
            
            for marker, description in html_markers:
                if marker in header_text:
                    return False, f"File appears to be HTML ({description})"
            
            # Check for common video file signatures
            video_signatures = [
                b'\x00\x00\x00',  # MP4/MOV start
                b'ftyp',          # MP4/MOV
                b'free',          # MOV
                b'mdat',          # MP4/MOV
                b'moov',          # MP4/MOV
                b'\xff\xff',      # Some codecs
                b'RIFF',          # AVI
                b'\x1a\x45\xdf\xa3',  # WebM
            ]
            
            has_video_signature = any(
                sig in header[:100] for sig in video_signatures
            )
            
            if not has_video_signature and len(header) > 100:
                warnings = []
                # Check if it's binary (likely video)
                non_printable = sum(1 for b in header[:500] if b > 127 or b < 32)
                if non_printable < 100:
                    return False, "File does not appear to be binary video data"
            
            return True, ""
            
        except Exception as e:
            return False, f"Cannot read file content: {e}"
    
    def _validate_with_ffprobe(self, file_path: Path) -> tuple[bool, str]:
        """
        Layer 3: Validate video with ffprobe.
        
        Verify the file has valid video codec information.
        """
        if not self.ffprobe_path:
            return True, ""  # Skip if ffprobe not available
        
        try:
            cmd = [
                str(self.ffprobe_path),
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(file_path)
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                error_msg = result.stderr.strip() or "Unknown ffprobe error"
                # Common errors that indicate invalid video
                if any(x in error_msg.lower() for x in ['invalid', 'error', 'moov', 'mdat']):
                    return False, f"ffprobe validation failed: {error_msg[:200]}"
            
            # Try to get video stream info
            cmd2 = [
                str(self.ffprobe_path),
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height",
                "-of", "csv=p=0",
                str(file_path)
            ]
            
            result2 = subprocess.run(
                cmd2,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result2.returncode != 0:
                return False, f"ffprobe cannot read video stream: {result2.stderr.strip()[:100]}"
            
            # Check if we got valid stream info
            stream_info = result2.stdout.strip()
            if not stream_info:
                return False, "ffprobe returned empty stream info"
            
            logger.debug(f"ffprobe stream info: {stream_info}")
            return True, ""
            
        except subprocess.TimeoutExpired:
            return False, "ffprobe validation timeout"
        except Exception as e:
            return False, f"ffprobe error: {e}"
    
    @staticmethod
    def quick_check(file_path: Path) -> bool:
        """
        Quick validation check (size + content only).
        
        Returns:
            True if file appears to be a valid video
        """
        if not file_path.exists():
            return False
        
        if file_path.stat().st_size < VideoValidator.MIN_FILE_SIZE:
            return False
        
        try:
            with open(file_path, 'rb') as f:
                header = f.read(500)
            
            header_lower = header.lower()
            html_markers = [b'<html', b'<!doctype', b'<!DOCTYPE']
            return not any(marker in header_lower for marker in html_markers)
        except:
            return False
