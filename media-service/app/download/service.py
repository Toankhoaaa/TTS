"""
Download Service - High-Level Download Interface

Combines downloader and validator for production-ready downloads.
"""

import uuid
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass

from .downloader import VideoDownloader, DownloadResult, DownloadStrategy, Platform
from .validator import VideoValidator, ValidationResult, ValidationLevel

logger = logging.getLogger(__name__)


@dataclass
class ServiceDownloadResult:
    """Result from download service."""
    success: bool
    job_id: Optional[str]
    file_path: Optional[str]
    file_size: int
    strategy_used: Optional[str]
    platform: Optional[str]
    validation_passed: bool
    error: Optional[str]
    metadata: Dict[str, Any]
    
    @property
    def human_readable_size(self) -> str:
        """Convert file size to human readable format."""
        size = self.file_size
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} TB"


class DownloadService:
    """
    Production-Ready Video Download Service.
    
    Features:
    - Multi-strategy download with fallback
    - 3-layer validation
    - Automatic cleanup on failure
    - Comprehensive logging
    """
    
    def __init__(
        self,
        output_dir: Path,
        venv_path: Optional[Path] = None,
        cookies_file: Optional[Path] = None,
        validation_level: ValidationLevel = ValidationLevel.STRICT,
    ):
        """
        Initialize download service.
        
        Args:
            output_dir: Directory to save downloaded videos
            venv_path: Path to virtual environment
            cookies_file: Path to cookies.txt file
            validation_level: How strict validation should be
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.venv_path = Path(venv_path) if venv_path else None
        self.cookies_file = cookies_file
        
        # Initialize components
        self.downloader = VideoDownloader(
            output_dir=self.output_dir,
            venv_path=self.venv_path,
            cookies_file=self.cookies_file,
        )
        
        self.validator = VideoValidator(
            venv_path=self.venv_path,
            level=validation_level,
        )
        
        # Job storage (in production, use Redis or database)
        self.jobs: Dict[str, Dict[str, Any]] = {}
        
        logger.info("DownloadService initialized")
        logger.info(f"  Output dir: {self.output_dir}")
        logger.info(f"  Venv path: {self.venv_path}")
        logger.info(f"  Cookies: {self.cookies_file}")
        logger.info(f"  Validation level: {validation_level.value}")
    
    def download(self, url: str) -> ServiceDownloadResult:
        """
        Download a video from URL with full validation.
        
        Args:
            url: Video URL from TikTok, YouTube, Twitter, etc.
            
        Returns:
            ServiceDownloadResult with download status and file info
        """
        # Generate job ID
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        
        logger.info("=" * 60)
        logger.info(f"Starting download job: {job_id}")
        logger.info(f"URL: {url}")
        
        # Detect platform
        platform = Platform.detect(url)
        logger.info(f"Platform detected: {platform.value}")
        
        # Step 1: Download video
        download_result = self.downloader.download(url, job_id)
        
        if not download_result.success or not download_result.file_path:
            logger.error(f"Download failed: {download_result.error}")
            return ServiceDownloadResult(
                success=False,
                job_id=None,
                file_path=None,
                file_size=0,
                strategy_used=download_result.strategy_used.value if download_result.strategy_used else None,
                platform=platform.value,
                validation_passed=False,
                error=download_result.error,
                metadata={"duration_seconds": download_result.duration_seconds},
            )
        
        logger.info(f"Download completed in {download_result.duration_seconds:.2f}s")
        logger.info(f"File: {download_result.file_path} ({download_result.human_readable_size})")
        logger.info(f"Strategy used: {download_result.strategy_used.value}")
        
        # Step 2: Validate downloaded file
        validation_result = self.validator.validate(download_result.file_path)
        
        if not validation_result.is_valid:
            logger.error("Validation failed:")
            for error in validation_result.errors:
                logger.error(f"  - {error}")
            
            # Cleanup failed download
            self._cleanup_file(download_result.file_path)
            
            return ServiceDownloadResult(
                success=False,
                job_id=None,
                file_path=None,
                file_size=download_result.file_size,
                strategy_used=download_result.strategy_used.value if download_result.strategy_used else None,
                platform=platform.value,
                validation_passed=False,
                error=f"Validation failed: {'; '.join(validation_result.errors)}",
                metadata={
                    "duration_seconds": download_result.duration_seconds,
                    "validation_errors": validation_result.errors,
                },
            )
        
        logger.info("Validation passed!")
        
        # Step 3: Register job (only if everything succeeded)
        self.jobs[job_id] = {
            "url": url,
            "platform": platform.value,
            "file_path": str(download_result.file_path),
            "file_size": download_result.file_size,
            "strategy_used": download_result.strategy_used.value if download_result.strategy_used else None,
            "metadata": {
                **download_result.metadata,
                "validation_warnings": validation_result.warnings,
            },
            "status": "ready",
        }
        
        logger.info(f"Job {job_id} registered successfully")
        logger.info("=" * 60)
        
        return ServiceDownloadResult(
            success=True,
            job_id=job_id,
            file_path=str(download_result.file_path),
            file_size=download_result.file_size,
            strategy_used=download_result.strategy_used.value if download_result.strategy_used else None,
            platform=platform.value,
            validation_passed=True,
            error=None,
            metadata=download_result.metadata,
        )
    
    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job info by ID."""
        return self.jobs.get(job_id)
    
    def get_job_file(self, job_id: str) -> Optional[Path]:
        """Get job file path if job exists and file is valid."""
        job = self.jobs.get(job_id)
        if not job:
            return None
        
        file_path = Path(job["file_path"])
        if not file_path.exists():
            return None
        
        return file_path
    
    def delete_job(self, job_id: str) -> bool:
        """Delete a job and its file."""
        job = self.jobs.get(job_id)
        if not job:
            return False
        
        file_path = Path(job["file_path"])
        if file_path.exists():
            try:
                file_path.unlink()
                logger.info(f"Deleted file: {file_path}")
            except Exception as e:
                logger.warning(f"Could not delete file: {e}")
        
        del self.jobs[job_id]
        return True
    
    def _cleanup_file(self, file_path: Optional[Path]) -> None:
        """Clean up a failed download file."""
        if file_path and file_path.exists():
            try:
                file_path.unlink()
                logger.info(f"Cleaned up failed file: {file_path}")
            except Exception as e:
                logger.warning(f"Could not clean up file: {e}")
    
    def list_jobs(self) -> Dict[str, Dict[str, Any]]:
        """List all jobs."""
        return self.jobs.copy()
    
    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """Clean up old jobs."""
        import time
        cutoff = time.time() - (max_age_hours * 3600)
        deleted = 0
        
        for job_id in list(self.jobs.keys()):
            job = self.jobs[job_id]
            # Simple cleanup based on status
            if job.get("status") == "failed":
                self.delete_job(job_id)
                deleted += 1
        
        logger.info(f"Cleaned up {deleted} old jobs")
        return deleted


# Global service instance (can be replaced with DI in production)
_global_service: Optional[DownloadService] = None


def get_download_service() -> DownloadService:
    """Get or create the global download service instance."""
    global _global_service
    
    if _global_service is None:
        from app.config import settings
        
        _global_service = DownloadService(
            output_dir=settings.videos_dir,
            venv_path=Path(__file__).parent.parent / "venv",
            cookies_file=None,
            validation_level=ValidationLevel.STRICT,
        )
    
    return _global_service
