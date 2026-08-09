from __future__ import annotations

import re
from pathlib import Path

import fitz

from .config import Settings
from .models import FixtureOptions, SimilarityProfile, SourceDocument


_IMAGE_PLACEHOLDER = re.compile(r"\[图片说明：.*?\]|!\[[^\]]*\]\([^)]*\)")
_MEANINGFUL = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_KNOWN_TERMS = (
    "无人机",
    "飞控",
    "硬件",
    "控制器",
    "传感器",
    "惯性测量",
    "通信",
    "电源",
    "电源树",
    "原理图",
    "电路板",
    "接口",
    "固件",
    "标定",
    "台架测试",
    "环回测试",
    "振动测试",
    "温升测试",
    "故障排查",
    "实验记录",
    "复现",
    "验证",
    "测试",
    "日志",
    "部署",
    "配置",
    "参数",
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
_ACTION_TERMS = ("设计", "选择", "配置", "测试", "验证", "标定", "测量", "记录", "分析", "结果", "结论", "故障", "修复")


class SimilarityProfileBuilder:
    """Builds a deterministic text-only profile. It never reads rendered pages or images."""

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
            raw_text, pdf_limit = self._extract_pdf_text(case_path, options)
            limitations.extend(pdf_limit)
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
        sentences = self._representative_sentences(cleaned, entities, parameters)
        summary = self._compose_summary(
            title=source.title,
            headings=headings,
            keywords=keywords,
            entities=entities,
            parameters=parameters,
            sentences=sentences,
            cleaned_body=cleaned,
        )
        return SimilarityProfile(
            document_id=source.document_id,
            title=source.title,
            source=source_kind,
            content=summary,
            headings=headings,
            keywords=keywords,
            technical_entities=entities,
            parameters=parameters,
            source_character_count=len(cleaned),
            summary_character_count=len(summary),
            limitations=limitations,
        )

    @staticmethod
    def _is_valid_blocks(markdown: str) -> bool:
        without_images = _IMAGE_PLACEHOLDER.sub("", markdown)
        without_markup = re.sub(r"[`#>*_|~\-]", "", without_images)
        return len(_MEANINGFUL.findall(without_markup)) >= 20

    @staticmethod
    def _is_valid_text(text: str) -> bool:
        return len(_MEANINGFUL.findall(text)) >= 20

    def _extract_pdf_text(self, case_path: Path, options: FixtureOptions) -> tuple[str, list[str]]:
        pdf_name = options.source_pdf or "source.pdf"
        pdf_path = case_path / pdf_name
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
        if not SimilarityProfileBuilder._is_valid_text(text):
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
        positioned: list[tuple[int, str]] = []
        for term in _KNOWN_TERMS:
            position = text.find(term)
            if position >= 0:
                positioned.append((position, term))
        positioned.sort(key=lambda item: (item[0], item[1]))
        heading_terms = [item for item in headings if 2 <= len(item) <= 20]
        return _unique([*heading_terms, *(item[1] for item in positioned), *entities], limit=30)

    @staticmethod
    def _representative_sentences(
        text: str, entities: list[str], parameters: list[str]
    ) -> list[str]:
        candidates: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        for order, raw in enumerate(re.split(r"(?<=[。！？!?；;])\s*|\n+", text)):
            sentence = re.sub(r"\s+", " ", raw).strip()
            key = re.sub(r"\s+", "", sentence).lower()
            if len(key) < 10 or key in seen:
                continue
            seen.add(key)
            entity_hits = sum(item.lower() in sentence.lower() for item in entities)
            parameter_hits = sum(item.lower() in sentence.lower() for item in parameters)
            action_hits = sum(item in sentence for item in _ACTION_TERMS)
            score = entity_hits * 8 + parameter_hits * 8 + action_hits * 3
            score += min(len(re.findall(r"\d", sentence)), 8) + min(len(sentence), 180) // 30
            candidates.append((score, order, sentence))
        selected = sorted(candidates, key=lambda item: (-item[0], item[1]))[:12]
        return [item[2] for item in sorted(selected, key=lambda item: item[1])]

    def _compose_summary(
        self,
        *,
        title: str,
        headings: list[str],
        keywords: list[str],
        entities: list[str],
        parameters: list[str],
        sentences: list[str],
        cleaned_body: str,
    ) -> str:
        max_chars = self.settings.similarity_summary_max_chars
        sections = [f"标题：{title}"]
        for label, values in (
            ("章节主题", headings),
            ("关键词", keywords),
            ("技术实体", entities),
            ("关键参数", parameters),
        ):
            if values:
                sections.append(f"{label}：{'、'.join(values)}")

        added_sentence = False
        for sentence in sentences:
            line = f"代表内容：{sentence}"
            candidate = "\n".join([*sections, line])
            if len(candidate) <= max_chars:
                sections.append(line)
                added_sentence = True
        if not added_sentence and cleaned_body:
            prefix = "正文摘要："
            remaining = max_chars - len("\n".join(sections)) - len(prefix) - 1
            if remaining > 0:
                sections.append(prefix + cleaned_body[:remaining].rstrip())
        return "\n".join(sections)[:max_chars].rstrip()


def _unique(values: object, *, limit: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in values:  # type: ignore[union-attr]
        value = str(raw).strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            output.append(value)
        if len(output) >= limit:
            break
    return output
