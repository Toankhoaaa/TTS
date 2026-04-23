"""
Download module for downloading videos from various platforms.
"""

from .validator import VideoValidator, ValidationResult, ValidationLevel
from .downloader import VideoDownloader, DownloadStrategy, DownloadResult, Platform
from .service import DownloadService, get_download_service

__all__ = [
    "VideoValidator",
    "ValidationResult",
    "ValidationLevel",
    "VideoDownloader",
    "DownloadStrategy",
    "DownloadResult",
    "Platform",
    "DownloadService",
    "get_download_service",
]
