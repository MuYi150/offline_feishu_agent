from __future__ import annotations

import hashlib
import re
from pathlib import Path

import fitz

from .config import Settings
from .models import FixtureOptions, SimilarityProfile, SourceDocument


_IMAGE_PLACEHOLDER = re.compile(r"\[图片说明：.*?\]|!\[[^\]]*\]\([^)]*\)")
_MEANINGFUL = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_KNOWN_TERMS = (
    "无人机", "飞控", "硬件", "控制器", "传感器", "惯性测量", "通信", "电源", "电源树",
    "原理图", "电路板", "接口", "固件", "标定", "台架测试", "环回测试", "振动测试",
    "温升测试", "故障排查", "实验记录", "复现", "验证", "测试", "日志", "部署", "配置", "参数",
)
_TECH_PATTERN = re.compile(
    r"(?i:\b(?:STM32[A-Z0-9-]*|ICM[-A-Z0-9]*|BMP\d+|CAN(?:-FD)?|UART|SPI|I2C|"
    r"USB|GNSS|GPS|IMU|PWM|ADC|DC-DC|LDO|PCB|EMI|EMC|PID|ROS2?|PX4|ArduPilot|"
    r"Python|Pytest|PowerShell)\b)|\b[A-Z]{2,}[A-Z0-9-]*\d+[A-Z0-9-]*\b"
)
_PARAMETER_PATTERN = re.compile(
    r"(?<!\w)[-+]?\d+(?:\.\d+)?(?:\s*[~～至-]\s*[-+]?\d+(?:\.\d+)?)?\s*"
    r"(?:V|mV|A|mA|W|kW|Hz|kHz|MHz|GHz|ms|s|min|mm|cm|m|km|g|kg|℃|°C|%|dB|rpm|Mbps|层|S)(?!\w)",
    re.I,
)


class SimilarityProfileBuilder:
    """Build the local query profile; rendered pages and images are never read."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build(
        self,
        *,
        source: SourceDocument,
        extracted_content: dict[str, object],
        case_path: Path,
        options: FixtureOptions,
    ) -> SimilarityProfile:
        markdown = str(extracted_content.get("content_markdown", ""))
        limitations: list[str] = []
        if self._is_valid_blocks(markdown):
            source_kind = "blocks"
            raw_text = markdown
        else:
            raw_text, pdf_limitations = self._extract_pdf_text(case_path, options)
            limitations.extend(pdf_limitations)
            source_kind = "pdf" if self._is_valid_text(raw_text) else "unavailable"

        if source_kind == "unavailable":
            if not limitations:
                limitations.append("blocks_and_native_pdf_text_unavailable")
            return SimilarityProfile(
                document_id=source.document_id,
                title=source.title,
                source="unavailable",
                limitations=limitations,
            )

        headings = self._headings(raw_text, source_kind)
        cleaned = self._clean_body(raw_text)
        entities = self._technical_entities(cleaned)
        parameters = self._parameters(cleaned)
        keywords = self._keywords(cleaned, headings, entities)
        query_text, truncated = self._compose_query(
            title=source.title,
            headings=headings,
            body=cleaned,
            keywords=keywords,
            entities=entities,
            parameters=parameters,
        )
        if truncated:
            limitations.append("similarity_query_truncated")
        return SimilarityProfile(
            document_id=source.document_id,
            title=source.title,
            source=source_kind,
            query_text=query_text,
            headings=headings,
            local_keywords=keywords,
            local_technical_entities=entities,
            local_key_parameters=parameters,
            source_character_count=len(cleaned),
            query_character_count=len(query_text),
            query_truncated=truncated,
            source_content_hash=self._content_hash(cleaned),
            limitations=limitations,
        )

    @staticmethod
    def _is_valid_blocks(markdown: str) -> bool:
        without_images = _IMAGE_PLACEHOLDER.sub("", markdown)
        without_markup = re.sub(r"[`#>*_|~\-]", "", without_images)
        return len(_MEANINGFUL.findall(without_markup)) >= 1

    @staticmethod
    def _is_valid_text(text: str) -> bool:
        return len(_MEANINGFUL.findall(text)) >= 1

    def _extract_pdf_text(self, case_path: Path, options: FixtureOptions) -> tuple[str, list[str]]:
        pdf_path = case_path / (options.source_pdf or "source.pdf")
        if not pdf_path.exists():
            return "", ["native_pdf_text_unavailable"]
        if pdf_path.stat().st_size > self.settings.max_pdf_bytes:
            return "", ["native_pdf_exceeds_size_limit"]
        try:
            with fitz.open(pdf_path) as document:
                if document.page_count > self.settings.max_pdf_pages:
                    return "", ["native_pdf_exceeds_page_limit"]
                text = "\n".join(page.get_text("text") for page in document)
        except Exception:
            return "", ["native_pdf_text_extraction_failed"]
        if not self._is_valid_text(text):
            return "", ["native_pdf_has_no_reliable_text"]
        return text, []

    @staticmethod
    def _headings(text: str, source_kind: str) -> list[str]:
        if source_kind == "blocks":
            found = [
                match.group(1).strip()
                for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", text, flags=re.M)
            ]
        else:
            found = [
                line.strip()
                for line in text.splitlines()
                if 2 <= len(line.strip()) <= 30 and not re.search(r"[。！？；.!?;]$", line.strip())
            ]
        return _unique(found, limit=20)

    @staticmethod
    def _clean_body(text: str) -> str:
        text = _IMAGE_PLACEHOLDER.sub("", text)
        text = re.sub(r"^```[^\n]*$|^```$", "", text, flags=re.M)
        text = text.replace("【表格】", "")
        cleaned_parts: list[str] = []
        seen: set[str] = set()
        for raw in re.split(r"\n\s*\n|\n", text):
            if re.match(r"^\s*#{1,6}\s+", raw):
                continue
            part = re.sub(r"^\s*(?:#{1,6}|[-+*>]|\d+[.)])\s*", "", raw)
            part = re.sub(r"\s+", " ", part).strip(" |\t")
            key = re.sub(r"\s+", "", part).lower()
            if not part or not key or key in seen:
                continue
            seen.add(key)
            cleaned_parts.append(part)
        return "\n".join(cleaned_parts)

    @staticmethod
    def _technical_entities(text: str) -> list[str]:
        return _unique((match.group(0) for match in _TECH_PATTERN.finditer(text)), limit=40)

    @staticmethod
    def _parameters(text: str) -> list[str]:
        return _unique((match.group(0).strip() for match in _PARAMETER_PATTERN.finditer(text)), limit=40)

    @staticmethod
    def _keywords(text: str, headings: list[str], entities: list[str]) -> list[str]:
        positioned = sorted(
            ((text.find(term), term) for term in _KNOWN_TERMS if term in text),
            key=lambda item: (item[0], item[1]),
        )
        heading_terms = [item for item in headings if 2 <= len(item) <= 20]
        return _unique([*heading_terms, *(item[1] for item in positioned), *entities], limit=30)

    def _compose_query(
        self,
        *,
        title: str,
        headings: list[str],
        body: str,
        keywords: list[str],
        entities: list[str],
        parameters: list[str],
    ) -> tuple[str, bool]:
        metadata = "\n".join(
            item
            for item in (
                f"标题：{title}",
                f"章节：{'、'.join(headings)}" if headings else "",
                f"关键词：{'、'.join(keywords)}" if keywords else "",
                f"技术实体：{'、'.join(entities)}" if entities else "",
                f"关键参数：{'、'.join(parameters)}" if parameters else "",
            )
            if item
        )
        full = f"{metadata}\n正文：\n{body}".strip()
        limit = self.settings.similarity_query_max_chars
        if len(full) <= limit:
            return full, False
        prefix = f"{metadata}\n正文（已按全文均匀取样）：\n"
        budget = max(1, limit - len(prefix))
        segments = _body_segments(body)
        if not segments:
            return prefix[:limit], True
        slot_count = min(len(segments), max(2, budget // 240))
        if slot_count == 1:
            indices = [0]
        else:
            indices = sorted({round(index * (len(segments) - 1) / (slot_count - 1)) for index in range(slot_count)})
        per_slot = max(1, (budget - max(0, len(indices) - 1)) // len(indices))
        sampled = "\n".join(segments[index][:per_slot] for index in indices)
        return (prefix + sampled)[:limit], True

    @staticmethod
    def _content_hash(text: str) -> str:
        normalized = re.sub(r"\s+", "", text).lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _body_segments(body: str) -> list[str]:
    segments: list[str] = []
    for line in body.splitlines():
        pieces = re.split(r"(?<=[。！？!?；;])", line)
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            segments.extend(piece[index : index + 500] for index in range(0, len(piece), 500))
    return segments


def _unique(values, *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = re.sub(r"\s+", " ", str(value)).strip()
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result
