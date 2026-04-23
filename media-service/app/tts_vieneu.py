"""
TTS Service using Edge TTS (Microsoft).

Supports specific voice names like vi-VN-HoaiMyNeural, vi-VN-NamMinhNeural.
"""

import logging
import asyncio
import os
from pathlib import Path
from typing import List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TTSSegment:
    """TTS segment with audio info."""
    index: int
    start: float
    end: float
    text: str
    audio_path: Optional[Path] = None
    duration: float = 0.0
    error: Optional[str] = None


class TTSService:
    """
    TTS Service using Edge TTS (Microsoft).
    Supports specific voice names for natural Vietnamese speech.
    """

    def __init__(self, voice: Optional[str] = None):
        self.voice = voice

    def _get_voice(self, language: str = 'vi') -> str:
        """Get voice to use - prefer explicit voice setting."""
        if self.voice:
            return self.voice
        # Default fallback
        voice_map = {
            'vi': 'vi-VN-HoaiMyNeural',
            'en': 'en-US-AriaNeural',
            'zh': 'zh-CN-XiaoxiaoNeural',
            'ja': 'ja-JP-NanamiNeural',
            'ko': 'ko-KR-SunhiNeural',
        }
        return voice_map.get(language, 'vi-VN-HoaiMyNeural')

    def _get_duration(self, audio_path: Path) -> float:
        """Get audio duration using ffprobe."""
        try:
            import subprocess
            cmd = [
                'C:\\ffmpeg\\bin\\ffprobe.exe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                str(audio_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            return float(result.stdout.strip())
        except Exception:
            return 0.0

    def _pad_audio_to_duration(self, audio_path: Path, target_duration: float) -> float:
        """Pad audio with silence to reach target duration."""
        import subprocess
        current = self._get_duration(audio_path)
        if current >= target_duration:
            return current

        pad_duration = target_duration - current
        padded_path = audio_path.with_suffix('.padded.mp3')

        cmd = [
            'C:\\ffmpeg\\bin\\ffmpeg.exe',
            '-y',
            '-i', str(audio_path),
            '-af', f'apad=whole_dur={target_duration:.3f}',
            '-acodec', 'libmp3lame',
            '-q:a', '2',
            str(padded_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and padded_path.exists():
            audio_path.unlink()
            padded_path.rename(audio_path)
            return target_duration
        return current

    def _generate_edge_tts(self, text: str, output_path: Path, target_duration: float, voice: str, max_retries: int = 3) -> Tuple[bool, float]:
        """Generate TTS using Edge TTS with retry logic."""
        try:
            from edge_tts import Communicate
        except ImportError:
            logger.error("edge-tts not installed. Run: pip install edge-tts")
            return False, 0.0

        for attempt in range(max_retries):
            try:
                asyncio.run(self._async_generate_edge_tts(text, output_path, voice))
                
                # Verify file was created and has content
                if not output_path.exists() or output_path.stat().st_size < 100:
                    raise Exception("Audio file too small or not created")
                
                actual_duration = self._get_duration(output_path)
                
                if actual_duration < 0.1:
                    raise Exception("Audio duration too short")
                
                # Pad if TTS is shorter than subtitle duration
                if actual_duration < target_duration - 0.05:
                    logger.info(f"TTS {actual_duration:.2f}s < target {target_duration:.2f}s, padding...")
                    actual_duration = self._pad_audio_to_duration(output_path, target_duration)
                
                return True, actual_duration
                
            except Exception as e:
                logger.warning(f"Edge TTS attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(1)  # Wait before retry
                    # Clean up failed file
                    if output_path.exists():
                        try:
                            output_path.unlink()
                        except:
                            pass
                else:
                    logger.error(f"Edge TTS failed after {max_retries} attempts: {e}")
                    return False, 0.0

    async def _async_generate_edge_tts(self, text: str, output_path: Path, voice: str):
        """Async helper for Edge TTS."""
        from edge_tts import Communicate
        communicate = Communicate(text, voice)
        await communicate.save(str(output_path))

    def generate_segments(self, segments: List, output_dir: Path, voice: str = None, language: str = 'vi') -> List[TTSSegment]:
        """Generate TTS for all subtitle segments."""
        output_dir.mkdir(parents=True, exist_ok=True)
        target_voice = voice or self._get_voice(language)

        print(f"[TTS] Processing {len(segments)} segments with voice: {target_voice}")
        print(f"[TTS] self.voice config: {self.voice}")
        print(f"[TTS] passed voice param: {voice}")
        logger.info(f"TTS voice: {target_voice}")

        tts_segments = []

        for seg in segments:
            text = seg.translated if hasattr(seg, 'translated') else (seg.text if hasattr(seg, 'text') else str(seg))
            output_path = output_dir / f"tts_{seg.index:04d}.mp3"

            tts_seg = TTSSegment(
                index=seg.index,
                start=seg.start,
                end=seg.end,
                text=text,
                audio_path=output_path
            )

            # Calculate target duration based on subtitle timing
            target_duration = seg.end - seg.start

            try:
                success, duration = self._generate_edge_tts(text, output_path, target_duration, target_voice)
                if success and duration > 0.2:
                    tts_seg.duration = duration
                    print(f"[TTS] OK [{seg.index}]: ({duration:.2f}s)")
                else:
                    tts_seg.error = "invalid audio"
                    print(f"[TTS] INVALID [{seg.index}]")
            except Exception as e:
                tts_seg.error = str(e)
                print(f"[TTS] FAIL [{seg.index}]: {e}")

            tts_segments.append(tts_seg)

        success_count = sum(1 for seg in tts_segments if not seg.error)
        print(f"[TTS] Results: {success_count}/{len(tts_segments)} successful")

        return tts_segments


def generate_tts_segments(
    segments: List,
    output_dir: Path,
    voice: Optional[str] = None,
    language: str = 'vi'
) -> List[TTSSegment]:
    """Generate TTS for subtitle segments."""
    service = TTSService(voice=voice)
    return service.generate_segments(segments, output_dir, voice, language)
