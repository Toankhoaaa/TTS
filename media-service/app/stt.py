"""
Speech-to-Text Module using faster-whisper.

Requirements:
- Input: Audio file (mp3, wav, m4a, etc.)
- Output: SRT subtitle file with EXACT transcription
- NO text modification, NO rewriting, NO summarization
"""

import logging
from pathlib import Path
from typing import List, Optional, Tuple
from dataclasses import dataclass

from faster_whisper import WhisperModel

from app.config import settings
from app.utils import format_timestamp, sanitize_text

logger = logging.getLogger(__name__)


@dataclass
class SubtitleSegment:
    """Single subtitle segment."""
    index: int
    start: float
    end: float
    text: str


class STTService:
    """Speech-to-Text service using faster-whisper."""

    _model_instance: Optional['STTService'] = None

    def __init__(self):
        self.model = None
        self._load_model()

    def _load_model(self) -> None:
        """Load Whisper model."""
        try:
            logger.info(f"Loading Whisper model: {settings.whisper_model}")
            self.model = WhisperModel(
                settings.whisper_model,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type
            )
            logger.info("Whisper model loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")
            raise

    @classmethod
    def get_instance(cls) -> 'STTService':
        """Get singleton instance."""
        if cls._model_instance is None:
            cls._model_instance = cls()
        return cls._model_instance

    def transcribe(
        self,
        audio_path: Path,
        language: Optional[str] = None,
        task: str = "transcribe"
    ) -> Tuple[List[SubtitleSegment], str]:
        """
        Transcribe audio to subtitle segments.

        Args:
            audio_path: Path to audio file
            language: Source language (auto-detect if None)
            task: "transcribe" or "translate"

        Returns:
            Tuple of (list of segments, detected language)
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        logger.info(f"Transcribing audio: {audio_path}")

        # Run inference with optimized parameters
        # VAD (Voice Activity Detection): DISABLED for short/noisy videos like TikTok
        # VAD tends to over-filter casual speech, music, and non-standard audio
        # Whisper's internal processing is sufficient for these use cases
        segments, info = self.model.transcribe(
            audio_path,
            language=language,
            task=task,
            beam_size=10,
            temperature=0,
            best_of=10,
            vad_filter=False,  # Disabled - was too aggressive
            initial_prompt="casual speech, social media, Chinese conversation"
        )

        # Collect segments
        subtitle_segments: List[SubtitleSegment] = []
        segment_list = list(segments)

        logger.info(f"Detected language: {info.language} (probability: {info.language_probability:.2f})")

        for idx, segment in enumerate(segment_list, start=1):
            # Get text directly from Whisper - no sanitization to preserve Chinese characters
            text = segment.text.strip()

            # Convert bytes to string if needed
            if isinstance(text, bytes):
                text = text.decode('utf-8', errors='ignore')

            # Skip ONLY truly empty segments (not short ones!)
            if not text:
                logger.warning(f"Segment {idx} is EMPTY - skipping")
                continue

            subtitle_segments.append(SubtitleSegment(
                index=idx,
                start=segment.start,
                end=segment.end,
                text=text
            ))

            # Log last segment for debugging
            if idx == len(segment_list):
                logger.info(f"LAST SEGMENT ({idx}): '{text}'")

            logger.debug(f"Segment {idx}: [{segment.start:.2f}-{segment.end:.2f}] {text[:50]}...")

        logger.info(f"Transcribed {len(subtitle_segments)} segments")
        return subtitle_segments, info.language

    def segments_to_srt(self, segments: List[SubtitleSegment]) -> str:
        """
        Convert segments to SRT format.

        IMPORTANT: NO text modification, NO rewriting.
        Output must match EXACTLY what Whisper outputs.
        """
        srt_lines = []

        for seg in segments:
            # Index
            srt_lines.append(str(seg.index))
            # Timestamp
            srt_lines.append(f"{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}")
            # Text (EXACT - no modification)
            srt_lines.append(seg.text)
            # Empty line
            srt_lines.append("")

        return "\n".join(srt_lines)

    def save_srt(self, segments: List[SubtitleSegment], output_path: Path) -> Path:
        """Save segments to SRT file."""
        srt_content = self.segments_to_srt(segments)
        output_path.write_text(srt_content, encoding="utf-8")
        logger.info(f"SRT saved to: {output_path}")
        return output_path

    def transcribe_to_srt(
        self,
        audio_path: Path,
        output_path: Optional[Path] = None,
        language: Optional[str] = None
    ) -> Tuple[Path, List[SubtitleSegment], str]:
        """
        Full transcription pipeline: audio -> SRT file.

        Returns:
            Tuple of (SRT path, segments, detected language)
        """
        segments, detected_lang = self.transcribe(audio_path, language=language)

        if output_path is None:
            output_path = audio_path.with_suffix(".srt")

        self.save_srt(segments, output_path)

        return output_path, segments, detected_lang


def transcribe_audio(
    audio_path: Path,
    output_srt: Optional[Path] = None,
    language: Optional[str] = None
) -> Tuple[Path, List[SubtitleSegment], str]:
    """
    Convenience function for transcription.

    Args:
        audio_path: Path to audio file
        output_srt: Output SRT path (optional)
        language: Source language (optional, auto-detect)

    Returns:
        Tuple of (SRT path, segments, detected language)
    """
    service = STTService.get_instance()
    return service.transcribe_to_srt(audio_path, output_srt, language)
