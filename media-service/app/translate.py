"""
Translate Module using Google Translate (deep-translator).

Pipeline chuẩn:
1. Merge segments (2-3 câu) - tránh bị cắt nhỏ
2. Translate theo block - KHÔNG từng câu
3. Preserve timing gốc
"""

import logging
import time
import re
from pathlib import Path
from typing import List, Optional, Tuple
from dataclasses import dataclass

from deep_translator import GoogleTranslator

from app.config import settings
from app.utils import format_timestamp

logger = logging.getLogger(__name__)


@dataclass
class TranslatedSegment:
    """Subtitle segment with translation."""
    index: int
    start: float
    end: float
    original: str
    translated: str


@dataclass
class MergedSegment:
    """Merged segment for better translation."""
    indices: List[int]  # Original segment indices
    start: float
    end: float
    text: str
    segments: List  # Original segment objects


LANG_CODE_MAP = {
    'en': 'en',
    'zh': 'zh-CN',
    'zh-cn': 'zh-CN',
    'zh-TW': 'zh-TW',
    'ja': 'ja',
    'ko': 'ko',
    'fr': 'fr',
    'de': 'de',
    'es': 'es',
    'vi': 'vi',
}


class TranslateService:
    """Translation service using Google Translate - Block-based."""

    def __init__(self):
        self._cache: dict = {}
        # Default source - will be auto-detected from Whisper
        self._source_lang = 'en'
        self._target_lang = 'vi'

    def set_languages(self, source: str, target: str = 'vi'):
        """Set source and target languages."""
        self._source_lang = LANG_CODE_MAP.get(source.lower(), source)
        self._target_lang = target
        logger.info(f"Translation: {self._source_lang} -> {self._target_lang}")

    def _clean_text(self, text: str) -> str:
        """Clean text before translation."""
        if not text:
            return ""
        import re
        text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]', '', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _merge_segments(
        self,
        segments: List,
        merge_size: int = 3,
        max_gap: float = 1.5
    ) -> List[MergedSegment]:
        """
        Merge consecutive segments into blocks for better translation.
        
        Rules:
        - Merge 2-3 segments together
        - Gap between segments should be < 1.5s
        - Don't merge if total text is too long (> 200 chars)
        """
        if not segments:
            return []

        merged = []
        current_group = []
        current_start = 0
        current_end = 0

        for i, seg in enumerate(segments):
            text = seg.text if hasattr(seg, 'text') else str(seg)
            start = getattr(seg, 'start', 0)
            end = getattr(seg, 'end', start + 1)

            if not current_group:
                # Start new group
                current_group = [seg]
                current_start = start
                current_end = end
            else:
                # Check if we should merge with current group
                gap = start - current_end
                combined_text = ' '.join(
                    g.text if hasattr(g, 'text') else str(g)
                    for g in current_group
                ) + ' ' + text

                should_merge = (
                    len(current_group) < merge_size and
                    gap < max_gap and
                    len(combined_text) < 200
                )

                if should_merge:
                    current_group.append(seg)
                    current_end = end
                else:
                    # Save current group
                    merged_text = ' '.join(
                        g.text if hasattr(g, 'text') else str(g)
                        for g in current_group
                    )
                    merged.append(MergedSegment(
                        indices=[g.index if hasattr(g, 'index') else i for g in current_group],
                        start=current_start,
                        end=current_end,
                        text=merged_text,
                        segments=current_group
                    ))
                    # Start new group
                    current_group = [seg]
                    current_start = start
                    current_end = end

        # Don't forget the last group
        if current_group:
            merged_text = ' '.join(
                g.text if hasattr(g, 'text') else str(g)
                for g in current_group
            )
            merged.append(MergedSegment(
                indices=[g.index if hasattr(g, 'index') else i for g in current_group],
                start=current_start,
                end=current_end,
                text=merged_text,
                segments=current_group
            ))

        logger.info(f"Merged {len(segments)} segments into {len(merged)} blocks")
        return merged

    def _translate_block(self, text: str) -> str:
        """Translate a block of text."""
        if not text or not text.strip():
            return text

        cache_key = text.strip()
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            clean_text = self._clean_text(text)
            result = GoogleTranslator(
                source=self._source_lang,
                target=self._target_lang
            ).translate(clean_text)

            if result:
                self._cache[cache_key] = result
                return result
            return clean_text

        except Exception as e:
            logger.warning(f"Translation failed for '{text[:50]}...': {e}")
            return text

    def _split_translation_back(self, translated: str, original_segments: List) -> List[str]:
        """
        Split translated block back to individual segments.
        Uses sentence-based splitting to avoid cutting words in half.
        """
        import re
        
        if len(original_segments) == 1:
            return [translated]

        # Split by sentence boundaries (., !, ?, 。, ！, ？)
        # Keep the delimiters with the preceding text
        sentences = re.split(r'(?<=[.!?。！？])\s+', translated.strip())
        
        if len(sentences) <= len(original_segments):
            # Fewer sentences than segments - distribute evenly
            result = []
            sent_per_seg = max(1, len(sentences) // len(original_segments))
            for i in range(0, len(sentences), sent_per_seg):
                chunk = ' '.join(sentences[i:i + sent_per_seg])
                result.append(chunk if chunk else sentences[min(i, len(sentences) - 1)])
            # Pad if needed
            while len(result) < len(original_segments):
                result.append(result[-1] if result else translated)
            return result[:len(original_segments)]
        
        # More sentences than segments - group them
        result = []
        seg_count = len(original_segments)
        sents_per_seg = len(sentences) // seg_count
        remainder = len(sentences) % seg_count
        
        idx = 0
        for i in range(seg_count):
            extra = 1 if i < remainder else 0
            count = sents_per_seg + extra
            chunk = ' '.join(sentences[idx:idx + count])
            result.append(chunk)
            idx += count
        
        return result

    def translate_segments(
        self,
        segments: List,
        source_lang: str = 'auto',
        merge_size: int = 3
    ) -> List[TranslatedSegment]:
        """
        Translate subtitle segments individually.
        Each segment is translated separately to avoid text distribution issues.
        """
        if not segments:
            return []

        # Set languages based on detected source
        self.set_languages(source_lang, 'vi')

        # Translate each segment individually
        result = []
        for i, seg in enumerate(segments, start=1):
            original_text = seg.text if hasattr(seg, 'text') else str(seg)
            translated_text = self._translate_block(original_text)
            
            result.append(TranslatedSegment(
                index=i,
                start=seg.start if hasattr(seg, 'start') else 0,
                end=seg.end if hasattr(seg, 'end') else 0,
                original=original_text,
                translated=translated_text
            ))

        logger.info(f"Translated {len(result)} segments individually")
        return result

    def translate_text(self, text: str, source_lang: str = 'en') -> str:
        """Translate single text block."""
        self.set_languages(source_lang, 'vi')
        return self._translate_block(text)

    def save_translated_srt(
        self,
        segments: List[TranslatedSegment],
        output_path: Path,
        include_original: bool = False
    ) -> Path:
        """Save translated segments to SRT file."""
        srt_lines = []

        for seg in segments:
            srt_lines.append(str(seg.index))
            srt_lines.append(f"{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}")
            srt_lines.append(seg.translated)

            if include_original and seg.original != seg.translated:
                srt_lines.append(f"[{seg.original}]")

            srt_lines.append("")

        output_path.write_text("\n".join(srt_lines), encoding="utf-8")
        logger.info(f"Translated SRT saved to: {output_path}")
        return output_path


def translate_subtitles(
    segments: List,
    source_lang: str = 'en',
    output_path: Optional[Path] = None,
    merge_size: int = 3
) -> Tuple[Path, List[TranslatedSegment]]:
    """Convenience function for subtitle translation."""
    service = TranslateService()
    translated = service.translate_segments(segments, source_lang, merge_size)

    if output_path is None:
        output_path = Path(f"translated_{segments[0].index if segments else 1}.srt")

    service.save_translated_srt(translated, output_path)
    return output_path, translated
