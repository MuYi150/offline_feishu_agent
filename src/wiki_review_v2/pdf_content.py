from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import fitz
from PIL import Image, ImageStat

from .config import Settings
from .errors import PdfError
from .models import (
    DocumentExtractionAudit,
    FixtureOptions,
    PageExtraction,
    PreparedDocumentInput,
    SourceDocument,
    VisualManifest,
    VisualPage,
    VisualSelectionAudit,
    VisualSelectionDecision,
)
from .pdf import PdfPageRenderer
from .storage import sha256_file


_MEANINGFUL = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_IMAGE_PLACEHOLDER = re.compile(r"\[图片说明：.*?\]|!\[[^\]]*\]\([^)]*\)")
_PAGE_NUMBER = re.compile(r"^(?:第\s*)?\d+(?:\s*页)?$", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HIGH_PRIORITY = re.compile(
    r"原理图|电路图|PCB|架构|流程图|框图|schematic|architecture|flow|测试|曲线|日志|波形|result|log|chart",
    re.I,
)
_UI_HINT = re.compile(r"界面|截图|配置|调试|安装|操作|QGC|GUI|console", re.I)


@dataclass
class _Candidate:
    item: VisualPage
    priority: int
    required: bool
    area_ratio: float
    nearby_text: str = ""


class PdfDocumentInputPreparer:
    """Prepare one unified text-and-visual input without model-assisted page selection."""

    def __init__(self, settings: Settings, renderer: PdfPageRenderer) -> None:
        self.settings = settings
        self.renderer = renderer

    def prepare(
        self,
        *,
        case_path: Path,
        output_dir: Path,
        source: SourceDocument,
        options: FixtureOptions,
        blocks_content: str,
        block_image_count: int = 0,
    ) -> tuple[
        PreparedDocumentInput,
        DocumentExtractionAudit,
        VisualSelectionAudit,
        VisualManifest,
        str,
    ]:
        try:
            pdf_path, source_kind = self._resolve_pdf(
                case_path, output_dir, options, blocks_content
            )
        except PdfError as exc:
            return self._unavailable(blocks_content, exc.safe_message)
        pages_dir = case_path / "pages"

        if pdf_path is None and pages_dir.is_dir():
            return self._prepare_fixture_pages(
                case_path, output_dir, source, options, blocks_content
            )
        if pdf_path is None:
            limitation = "结构化正文和 PDF/页面图片均不可用"
            return self._unavailable(blocks_content, limitation)

        try:
            with fitz.open(pdf_path) as document:
                total_pages = document.page_count
        except Exception:
            return self._unavailable(blocks_content, "PDF 损坏或无法打开")
        if total_pages <= 0:
            raise PdfError("PDF 不包含页面")
        if total_pages > self.settings.max_pdf_pages:
            limitation = (
                f"PDF 共 {total_pages} 页，超过上限 {self.settings.max_pdf_pages}，未静默截断。"
            )
            return self._unavailable(blocks_content, limitation, total_pages=total_pages)

        if total_pages <= self.settings.direct_page_limit:
            manifest, reason = self.renderer.prepare(
                case_path, output_dir, source, options, blocks_content
            )
            image_bytes = sum(page.byte_size for page in manifest.rendered_pages)
            if (
                len(manifest.rendered_pages) <= self.settings.direct_image_count_limit
                and image_bytes <= self.settings.direct_image_bytes_limit
            ):
                return self._legacy_result(
                    pdf_path=pdf_path,
                    source_kind=source_kind,
                    blocks_content=blocks_content,
                    manifest=manifest,
                    reason=reason,
                )

        if self.settings.long_pdf_legacy_batch_fallback:
            manifest, reason = self.renderer.prepare(
                case_path, output_dir, source, options, blocks_content
            )
            return self._legacy_result(
                pdf_path=pdf_path,
                source_kind=source_kind,
                blocks_content=blocks_content,
                manifest=manifest,
                reason=reason,
                mode="legacy_batch_fallback",
            )

        return self._selective_pdf(
            pdf_path=pdf_path,
            output_dir=output_dir,
            source_kind=source_kind,
            blocks_content=blocks_content,
            block_image_count=block_image_count,
        )

    def _resolve_pdf(
        self,
        case_path: Path,
        output_dir: Path,
        options: FixtureOptions,
        blocks_content: str,
    ) -> tuple[Path | None, str]:
        configured = options.source_pdf
        conventional = case_path / "source.pdf"
        if configured or conventional.exists():
            path = case_path / (configured or "source.pdf")
            if not path.exists():
                raise PdfError(f"声明的 PDF 不存在：{path.name}")
            if path.stat().st_size > self.settings.max_pdf_bytes:
                raise PdfError("PDF 文件大小超过安全上限")
            return path, "pdf"
        if options.pdf_required:
            raise PdfError("Fixture 要求提供 PDF，但未声明 source_pdf")
        if not blocks_content.strip():
            return None, "unavailable"
        generated = output_dir / "generated_source.pdf"
        self.renderer.generate_pdf(blocks_content, generated)
        return generated, "generated_pdf"

    def _legacy_result(
        self,
        *,
        pdf_path: Path,
        source_kind: str,
        blocks_content: str,
        manifest: VisualManifest,
        reason: str,
        mode: str = "legacy_full_pages",
    ) -> tuple[PreparedDocumentInput, DocumentExtractionAudit, VisualSelectionAudit, VisualManifest, str]:
        pages, failed = self._text_audit(pdf_path)
        source_name = "blocks" if self._valid_blocks(blocks_content) else "pdf"
        content = blocks_content if source_name == "blocks" else self._compose_pdf_text(pages)
        items = [
            page.model_copy(update={"type": "full_page", "selection_reason": "legacy_all_pages"})
            for page in manifest.rendered_pages
        ]
        manifest = manifest.model_copy(update={"rendered_pages": items})
        extraction = DocumentExtractionAudit(
            total_pages=manifest.total_pages,
            structured_content_source=source_name if content.strip() else "unavailable",
            text_pages_covered=sum(page.text_source == "native" for page in pages),
            pages=pages,
            scanned_pages=[page.page for page in pages if page.scanned],
            failed_pages=failed,
            limitations=list(manifest.limitations),
        )
        decisions = [
            VisualSelectionDecision(
                item=item,
                status="selected",
                reason="short_document_all_pages",
                priority=100,
                required_for_review=True,
            )
            for item in items
        ]
        selection = VisualSelectionAudit(
            mode=mode,
            discovered=items,
            decisions=decisions,
            selected=items,
            full_page_fallbacks=[],
            selected_count=len(items),
            selected_bytes=sum(item.byte_size for item in items),
            limitations=list(manifest.limitations),
        )
        prepared = PreparedDocumentInput(
            mode=mode,
            structured_content=content,
            structured_content_source=source_name if content.strip() else "unavailable",
            visual_items=items,
            total_pages=manifest.total_pages,
            text_pages_covered=extraction.text_pages_covered,
            visual_regions_found=len(items),
            visual_regions_selected=len(items),
            visual_coverage_complete=(
                bool(manifest.total_pages)
                and not manifest.failed_pages
                and len(items) == manifest.total_pages
            ),
            limitations=list(manifest.limitations),
        )
        return prepared, extraction, selection, manifest, reason

    def _selective_pdf(
        self,
        *,
        pdf_path: Path,
        output_dir: Path,
        source_kind: str,
        blocks_content: str,
        block_image_count: int,
    ) -> tuple[PreparedDocumentInput, DocumentExtractionAudit, VisualSelectionAudit, VisualManifest, str]:
        regions_dir = output_dir / "visual_regions"
        regions_dir.mkdir(parents=True, exist_ok=True)
        try:
            document = fitz.open(pdf_path)
        except Exception as exc:
            raise PdfError("PDF 损坏或无法打开") from exc
        try:
            raw_pages = [self._raw_page_text(page) for page in document]
            repeated = self._repeated_marginal_lines(raw_pages)
            pages: list[PageExtraction] = []
            candidates: list[_Candidate] = []
            failed_pages: list[int] = []
            supplemental_tables: list[tuple[int, str]] = []

            for index, page in enumerate(document):
                page_no = index + 1
                try:
                    page_result, page_candidates = self._extract_page(
                        page,
                        page_no=page_no,
                        raw_lines=raw_pages[index],
                        repeated_margins=repeated,
                        regions_dir=regions_dir,
                    )
                    pages.append(page_result)
                    candidates.extend(page_candidates)
                    supplemental_tables.extend(
                        (page_no, table) for table in page_result.simple_tables
                    )
                except Exception:
                    failed_pages.append(page_no)
                    pages.append(
                        PageExtraction(
                            page=page_no,
                            text_source="error",
                            extraction_failed=True,
                            limitations=["page_extraction_failed"],
                        )
                    )

            selected, selection = self._select_candidates(candidates)
            blocks_reliable = self._valid_blocks(blocks_content)
            if blocks_reliable:
                content = blocks_content
                additions = [
                    (page, table)
                    for page, table in supplemental_tables
                    if self._normalize_text(table) not in self._normalize_text(blocks_content)
                ]
                if additions:
                    content += "\n\n## PDF 提取的补充表格\n" + "\n\n".join(
                        f"[PDF第{page}页]\n{table}" for page, table in additions
                    )
                content_source = "blocks"
            else:
                content = self._compose_pdf_text(pages)
                content_source = "pdf" if content.strip() else "unavailable"

            limitations = list(selection.limitations)
            if source_kind == "generated_pdf":
                # Generated pages repeat Blocks and are not independent evidence.
                selected = []
                selection = selection.model_copy(
                    update={
                        "selected": [],
                        "selected_count": 0,
                        "selected_bytes": 0,
                        "limitations": list(dict.fromkeys([*limitations, "generated_pdf_not_independent_visual_evidence"])),
                    }
                )
                if block_image_count:
                    selection = selection.model_copy(
                        update={
                            "required_visuals_omitted": True,
                            "budget_exceeded": False,
                            "limitations": [*selection.limitations, "blocks_reference_images_but_original_visuals_unavailable"],
                        }
                    )
            required_omitted = selection.required_visuals_omitted
            extraction_limitations = []
            if failed_pages:
                extraction_limitations.append(f"页面提取失败：{failed_pages}")
            extraction = DocumentExtractionAudit(
                total_pages=document.page_count,
                structured_content_source=content_source,
                text_pages_covered=sum(page.text_source == "native" for page in pages),
                pages=pages,
                scanned_pages=[page.page for page in pages if page.scanned],
                failed_pages=failed_pages,
                limitations=extraction_limitations,
            )
            visual_complete = not failed_pages and not required_omitted
            if source_kind == "generated_pdf" and not block_image_count:
                visual_complete = True
            all_limitations = list(
                dict.fromkeys([*extraction.limitations, *selection.limitations])
            )
            manifest = VisualManifest(
                source=source_kind,
                total_pages=document.page_count,
                rendered_pages=selected,
                failed_pages=failed_pages,
                limitations=all_limitations,
            )
            prepared = PreparedDocumentInput(
                mode="selective_regions",
                structured_content=content,
                structured_content_source=content_source,
                visual_items=selected,
                total_pages=document.page_count,
                text_pages_covered=extraction.text_pages_covered,
                visual_regions_found=len(selection.discovered),
                visual_regions_selected=len(selected),
                full_page_fallbacks=selection.full_page_fallbacks,
                visual_coverage_complete=visual_complete,
                limitations=all_limitations,
            )
            reason = ""
            if not content.strip() and not selected:
                reason = "PDF 没有可靠原生文字或可用视觉证据"
            return prepared, extraction, selection, manifest, reason
        finally:
            document.close()

    def _extract_page(
        self,
        page: fitz.Page,
        *,
        page_no: int,
        raw_lines: list[tuple[str, tuple[float, float, float, float]]],
        repeated_margins: set[str],
        regions_dir: Path,
    ) -> tuple[PageExtraction, list[_Candidate]]:
        page_rect = page.rect
        page_area = max(1.0, page_rect.width * page_rect.height)
        cleaned_lines = []
        for text, bbox in raw_lines:
            normalized = self._normalize_text(text)
            if not normalized or normalized in repeated_margins:
                continue
            if _PAGE_NUMBER.fullmatch(normalized):
                continue
            cleaned_lines.append((self._clean_text(text), fitz.Rect(bbox)))

        blocks = page.get_text("dict").get("blocks", [])
        image_blocks = [block for block in blocks if block.get("type") == 1 and block.get("bbox")]
        tables = self._find_tables(page)
        simple_tables: list[str] = []
        table_rects: list[fitz.Rect] = []
        candidates: list[_Candidate] = []
        complex_count = 0

        for table_index, table in enumerate(tables, 1):
            bbox = fitz.Rect(table.bbox)
            table_rects.append(bbox)
            matrix = self._table_matrix(table)
            if self._simple_table(matrix, bbox, image_blocks):
                simple_tables.append(self._table_markdown(matrix))
            else:
                complex_count += 1
                item = self._render_region(
                    page,
                    bbox,
                    regions_dir / f"page-{page_no}-table-{table_index}.png",
                    evidence_id=f"page-{page_no}-table-{table_index}",
                    page_no=page_no,
                    item_type="table_crop",
                    reason="complex_table_requires_visual_review",
                )
                candidates.append(_Candidate(item, 80, True, self._area_ratio(bbox, page_area)))

        for bbox in table_rects:
            cleaned_lines = [(text, rect) for text, rect in cleaned_lines if not self._inside(rect, bbox)]

        embedded_count = 0
        large_image = False
        for image_index, block in enumerate(image_blocks, 1):
            bbox = fitz.Rect(block["bbox"]) & page_rect
            ratio = self._area_ratio(bbox, page_area)
            if ratio >= 0.40:
                large_image = True
            if ratio < 0.01 or bbox.width < 64 or bbox.height < 64:
                continue
            item = self._render_region(
                page,
                bbox,
                regions_dir / f"page-{page_no}-image-{image_index}.png",
                evidence_id=f"page-{page_no}-image-{image_index}",
                page_no=page_no,
                item_type="embedded_image",
                reason="embedded_image_block",
            )
            embedded_count += 1
            nearby = self._nearby_text(cleaned_lines, bbox)
            priority = 90 if _HIGH_PRIORITY.search(nearby) else 70 if _UI_HINT.search(nearby) else 40
            required = priority >= 70 or ratio >= 0.20
            candidates.append(_Candidate(item, priority, required, ratio, nearby))

        drawings = page.get_drawings()
        diagram_rects = self._drawing_regions(drawings, page_rect)
        diagram_count = 0
        for diagram_index, bbox in enumerate(diagram_rects, 1):
            if any(self._overlap_ratio(bbox, other) > 0.5 for other in table_rects):
                continue
            if any(self._overlap_ratio(bbox, fitz.Rect(block["bbox"])) > 0.5 for block in image_blocks):
                continue
            ratio = self._area_ratio(bbox, page_area)
            nearby = self._nearby_text(cleaned_lines, bbox)
            if ratio > 0.85 or (
                bbox.x0 <= page_rect.x0 + 1
                or bbox.y0 <= page_rect.y0 + 1
                or bbox.x1 >= page_rect.x1 - 1
                or bbox.y1 >= page_rect.y1 - 1
            ):
                continue
            item = self._render_region(
                page,
                bbox,
                regions_dir / f"page-{page_no}-diagram-{diagram_index}.png",
                evidence_id=f"page-{page_no}-diagram-{diagram_index}",
                page_no=page_no,
                item_type="diagram_crop",
                reason="vector_drawing_region",
            )
            diagram_count += 1
            priority = 100 if _HIGH_PRIORITY.search(nearby) else 85
            candidates.append(_Candidate(item, priority, True, ratio, nearby))

        native_text = "\n".join(text for text, _ in cleaned_lines if text).strip()
        native_reliable = len(_MEANINGFUL.findall(native_text)) >= 20
        scanned = not native_reliable and (large_image or bool(diagram_rects))
        if scanned:
            # A scan is reviewed as a page, not as an isolated embedded bitmap.
            candidates = [candidate for candidate in candidates if candidate.item.type != "embedded_image"]
            item = self._render_region(
                page,
                page_rect,
                regions_dir / f"page-{page_no}-full.png",
                evidence_id=f"page-{page_no}-full",
                page_no=page_no,
                item_type="scanned_page_fallback",
                reason="native_text_unavailable_full_page_fallback",
            )
            candidates.append(_Candidate(item, 60, True, 1.0))

        text_source = "native" if native_reliable else "scanned_or_unavailable" if scanned else "blank"
        return (
            PageExtraction(
                page=page_no,
                text_source=text_source,
                text=native_text if native_reliable else "",
                character_count=len(native_text) if native_reliable else 0,
                simple_tables=simple_tables,
                simple_table_count=len(simple_tables),
                complex_table_count=complex_count,
                embedded_image_count=embedded_count,
                diagram_count=diagram_count,
                scanned=scanned,
                limitations=[] if native_reliable or not scanned else ["native_text_unavailable"],
            ),
            candidates,
        )

    def _select_candidates(
        self, candidates: list[_Candidate]
    ) -> tuple[list[VisualPage], VisualSelectionAudit]:
        ordered = sorted(
            candidates,
            key=lambda item: (-item.priority, -item.area_ratio, item.item.page, item.item.evidence_id),
        )
        decisions: list[VisualSelectionDecision] = []
        unique: list[_Candidate] = []
        hashes: dict[str, _Candidate] = {}
        perceptual: list[tuple[int, float, _Candidate]] = []

        for candidate in ordered:
            item = candidate.item
            if self._near_solid(item.path):
                decisions.append(self._decision(candidate, "filtered", "near_solid_or_decorative"))
                continue
            exact = hashes.get(item.sha256)
            if exact is not None:
                decisions.append(
                    self._decision(candidate, "deduplicated", "duplicate_sha256", exact.item.evidence_id)
                )
                continue
            dhash, aspect = self._dhash(item.path)
            duplicate = next(
                (
                    existing
                    for old_hash, old_aspect, existing in perceptual
                    if abs(aspect - old_aspect) <= 0.05 * max(aspect, old_aspect, 0.01)
                    and (dhash ^ old_hash).bit_count() <= 5
                ),
                None,
            )
            if duplicate is not None:
                decisions.append(
                    self._decision(candidate, "deduplicated", "duplicate_dhash", duplicate.item.evidence_id)
                )
                continue
            hashes[item.sha256] = candidate
            perceptual.append((dhash, aspect, candidate))
            unique.append(candidate)

        selected: list[VisualPage] = []
        total_bytes = 0
        fallback_count = 0
        required_omitted = False
        budget_exceeded = False
        for candidate in unique:
            is_fallback = candidate.item.type in {"full_page", "scanned_page_fallback"}
            fits = (
                len(selected) < self.settings.max_visual_regions
                and total_bytes + candidate.item.byte_size <= self.settings.max_visual_total_bytes
                and (not is_fallback or fallback_count < self.settings.max_full_page_fallbacks)
            )
            if fits:
                selected.append(candidate.item)
                total_bytes += candidate.item.byte_size
                fallback_count += int(is_fallback)
                decisions.append(self._decision(candidate, "selected", "selected_by_priority"))
            else:
                budget_exceeded = True
                required_omitted = required_omitted or candidate.required
                decisions.append(self._decision(candidate, "budget_omitted", "visual_budget_exceeded"))

        limitations: list[str] = []
        if budget_exceeded:
            limitations.append("部分视觉区域因数量、字节或整页兜底预算未发送")
        if required_omitted:
            limitations.append("重要视觉区域未能完整提交，视觉覆盖为 partial")
        audit = VisualSelectionAudit(
            mode="selective_regions",
            discovered=[candidate.item for candidate in ordered],
            decisions=decisions,
            selected=selected,
            full_page_fallbacks=[
                item.page for item in selected if item.type in {"full_page", "scanned_page_fallback"}
            ],
            selected_count=len(selected),
            selected_bytes=total_bytes,
            budget_exceeded=budget_exceeded,
            required_visuals_omitted=required_omitted,
            limitations=limitations,
        )
        return selected, audit

    def _prepare_fixture_pages(
        self,
        case_path: Path,
        output_dir: Path,
        source: SourceDocument,
        options: FixtureOptions,
        blocks_content: str,
    ) -> tuple[PreparedDocumentInput, DocumentExtractionAudit, VisualSelectionAudit, VisualManifest, str]:
        manifest, reason = self.renderer.prepare(case_path, output_dir, source, options, blocks_content)
        if (
            manifest.total_pages <= self.settings.direct_page_limit
            and len(manifest.rendered_pages) <= self.settings.direct_image_count_limit
            and sum(item.byte_size for item in manifest.rendered_pages)
            <= self.settings.direct_image_bytes_limit
        ):
            fake_pdf = output_dir / "fixture-pages-no-pdf"
            pages = [
                PageExtraction(page=index, text_source="scanned_or_unavailable", scanned=True)
                for index in range(1, manifest.total_pages + 1)
            ]
            items = [
                item.model_copy(update={"type": "full_page", "selection_reason": "legacy_all_pages"})
                for item in manifest.rendered_pages
            ]
            extraction = DocumentExtractionAudit(
                total_pages=manifest.total_pages,
                structured_content_source="blocks" if self._valid_blocks(blocks_content) else "unavailable",
                pages=pages,
                scanned_pages=list(range(1, manifest.total_pages + 1)),
            )
            selection = VisualSelectionAudit(
                mode="legacy_full_pages",
                discovered=items,
                decisions=[
                    VisualSelectionDecision(
                        item=item,
                        status="selected",
                        reason="short_document_all_pages",
                        priority=100,
                        required_for_review=True,
                    )
                    for item in items
                ],
                selected=items,
                selected_count=len(items),
                selected_bytes=sum(item.byte_size for item in items),
            )
            manifest = manifest.model_copy(update={"rendered_pages": items})
            prepared = PreparedDocumentInput(
                mode="legacy_full_pages",
                structured_content=blocks_content,
                structured_content_source="blocks" if self._valid_blocks(blocks_content) else "unavailable",
                visual_items=items,
                total_pages=manifest.total_pages,
                visual_regions_found=len(items),
                visual_regions_selected=len(items),
                visual_coverage_complete=not manifest.failed_pages and len(items) == manifest.total_pages,
            )
            return prepared, extraction, selection, manifest, reason

        candidates = [
            _Candidate(
                item.model_copy(
                    update={
                        "type": "scanned_page_fallback",
                        "selection_reason": "fixture_page_without_native_pdf",
                    }
                ),
                60,
                True,
                1.0,
            )
            for item in manifest.rendered_pages
        ]
        selected, selection = self._select_candidates(candidates)
        pages = [
            PageExtraction(page=index, text_source="scanned_or_unavailable", scanned=True)
            for index in range(1, manifest.total_pages + 1)
        ]
        extraction = DocumentExtractionAudit(
            total_pages=manifest.total_pages,
            structured_content_source="blocks" if self._valid_blocks(blocks_content) else "unavailable",
            pages=pages,
            scanned_pages=list(range(1, manifest.total_pages + 1)),
            failed_pages=manifest.failed_pages,
            limitations=["fixture_pages_have_no_native_pdf_text"],
        )
        limitations = list(dict.fromkeys([*extraction.limitations, *selection.limitations]))
        manifest = manifest.model_copy(update={"rendered_pages": selected, "limitations": limitations})
        prepared = PreparedDocumentInput(
            mode="selective_regions",
            structured_content=blocks_content,
            structured_content_source="blocks" if self._valid_blocks(blocks_content) else "unavailable",
            visual_items=selected,
            total_pages=manifest.total_pages,
            visual_regions_found=len(candidates),
            visual_regions_selected=len(selected),
            full_page_fallbacks=selection.full_page_fallbacks,
            visual_coverage_complete=not manifest.failed_pages and not selection.required_visuals_omitted,
            limitations=limitations,
        )
        return prepared, extraction, selection, manifest, reason

    def _unavailable(
        self, blocks_content: str, limitation: str, *, total_pages: int = 0
    ) -> tuple[PreparedDocumentInput, DocumentExtractionAudit, VisualSelectionAudit, VisualManifest, str]:
        content_source = "blocks" if self._valid_blocks(blocks_content) else "unavailable"
        extraction = DocumentExtractionAudit(
            total_pages=total_pages,
            structured_content_source=content_source,
            limitations=[limitation],
        )
        selection = VisualSelectionAudit(
            mode="selective_regions", limitations=[limitation], required_visuals_omitted=True
        )
        prepared = PreparedDocumentInput(
            mode="selective_regions",
            structured_content=blocks_content,
            structured_content_source=content_source,
            total_pages=total_pages,
            visual_coverage_complete=False,
            limitations=[limitation],
        )
        manifest = VisualManifest(source="unavailable", total_pages=total_pages, limitations=[limitation])
        return prepared, extraction, selection, manifest, limitation

    def _text_audit(self, pdf_path: Path) -> tuple[list[PageExtraction], list[int]]:
        pages: list[PageExtraction] = []
        failed: list[int] = []
        try:
            with fitz.open(pdf_path) as document:
                raw = [self._raw_page_text(page) for page in document]
                repeated = self._repeated_marginal_lines(raw)
                for index, lines in enumerate(raw, 1):
                    text = "\n".join(
                        self._clean_text(value)
                        for value, _ in lines
                        if self._normalize_text(value) not in repeated
                        and not _PAGE_NUMBER.fullmatch(self._normalize_text(value))
                    ).strip()
                    reliable = len(_MEANINGFUL.findall(text)) >= 20
                    pages.append(
                        PageExtraction(
                            page=index,
                            text_source="native" if reliable else "blank",
                            text=text if reliable else "",
                            character_count=len(text) if reliable else 0,
                        )
                    )
        except Exception:
            failed = [1]
        return pages, failed

    @staticmethod
    def _raw_page_text(page: fitz.Page) -> list[tuple[str, tuple[float, float, float, float]]]:
        result: list[tuple[str, tuple[float, float, float, float]]] = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(span.get("text", "") for span in line.get("spans", []))
                bbox = tuple(line.get("bbox", block.get("bbox", (0, 0, 0, 0))))
                result.append((text, bbox))
        return result

    def _repeated_marginal_lines(
        self, pages: list[list[tuple[str, tuple[float, float, float, float]]]]
    ) -> set[str]:
        counter: Counter[str] = Counter()
        for lines in pages:
            seen: set[str] = set()
            # A4 is the dominant source format; keeping a page-height floor avoids
            # treating the last body line as a footer on sparse pages.
            max_y = max(842.0, max((bbox[3] for _, bbox in lines), default=0.0))
            for text, bbox in lines:
                normalized = self._normalize_text(text)
                if normalized and (bbox[1] <= max_y * 0.08 or bbox[3] >= max_y * 0.92):
                    seen.add(normalized)
            counter.update(seen)
        threshold = max(2, math.ceil(len(pages) / 2))
        return {text for text, count in counter.items() if count >= threshold}

    @staticmethod
    def _find_tables(page: fitz.Page) -> list[Any]:
        try:
            return list(page.find_tables().tables)
        except Exception:
            return []

    @staticmethod
    def _table_matrix(table: Any) -> list[list[str]]:
        try:
            return [[str(cell or "").strip() for cell in row] for row in table.extract()]
        except Exception:
            return []

    @staticmethod
    def _simple_table(
        matrix: list[list[str]], bbox: fitz.Rect, image_blocks: list[dict[str, Any]]
    ) -> bool:
        if len(matrix) < 2 or not matrix or len(matrix[0]) < 2:
            return False
        columns = len(matrix[0])
        if columns > 20 or len(matrix) > 100 or any(len(row) != columns for row in matrix):
            return False
        cells = [cell for row in matrix for cell in row]
        if sum(bool(cell) for cell in cells) / len(cells) < 0.8:
            return False
        if any(len(cell) > 300 for cell in cells):
            return False
        return not any(
            PdfDocumentInputPreparer._overlap_ratio(bbox, fitz.Rect(block["bbox"])) > 0.05
            for block in image_blocks
        )

    @staticmethod
    def _table_markdown(matrix: list[list[str]]) -> str:
        def cell(value: str) -> str:
            return value.replace("|", "\\|").replace("\n", "<br>").strip()

        header = "| " + " | ".join(cell(value) for value in matrix[0]) + " |"
        divider = "| " + " | ".join("---" for _ in matrix[0]) + " |"
        rows = ["| " + " | ".join(cell(value) for value in row) + " |" for row in matrix[1:]]
        return "\n".join([header, divider, *rows])

    @staticmethod
    def _drawing_regions(drawings: list[dict[str, Any]], page_rect: fitz.Rect) -> list[fitz.Rect]:
        rects = [fitz.Rect(item["rect"]) & page_rect for item in drawings if item.get("rect")]
        rects = [rect for rect in rects if rect.width * rect.height >= page_rect.width * page_rect.height * 0.005]
        if len(rects) < 4:
            return []
        expanded = [rect + (-12, -12, 12, 12) for rect in rects]
        groups: list[list[fitz.Rect]] = []
        for rect in expanded:
            for group in groups:
                union = group[0]
                for member in group[1:]:
                    union |= member
                if union.intersects(rect):
                    group.append(rect)
                    break
            else:
                groups.append([rect])
        result: list[fitz.Rect] = []
        page_area = page_rect.width * page_rect.height
        for group in groups:
            if len(group) < 4:
                continue
            union = group[0]
            for rect in group[1:]:
                union |= rect
            union &= page_rect
            if 0.05 <= union.width * union.height / page_area <= 0.90:
                result.append(union)
        return result

    def _render_region(
        self,
        page: fitz.Page,
        bbox: fitz.Rect,
        target: Path,
        *,
        evidence_id: str,
        page_no: int,
        item_type: str,
        reason: str,
    ) -> VisualPage:
        bbox &= page.rect
        zoom = self.settings.pdf_render_dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=bbox, alpha=False)
        if max(pix.width, pix.height) > self.settings.max_image_long_edge:
            ratio = self.settings.max_image_long_edge / max(pix.width, pix.height)
            pix = page.get_pixmap(
                matrix=fitz.Matrix(zoom * ratio, zoom * ratio), clip=bbox, alpha=False
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        pix.save(target)
        return VisualPage(
            evidence_id=evidence_id,
            page=page_no,
            path=str(target.resolve()),
            width=pix.width,
            height=pix.height,
            byte_size=target.stat().st_size,
            sha256=sha256_file(target),
            type=item_type,
            bbox=[round(value, 2) for value in (bbox.x0, bbox.y0, bbox.x1, bbox.y1)],
            selection_reason=reason,
        )

    @staticmethod
    def _near_solid(path: str) -> bool:
        try:
            with Image.open(path) as image:
                stat = ImageStat.Stat(image.convert("RGB").resize((32, 32)))
                return max(stat.stddev) < 3.0
        except Exception:
            return False

    @staticmethod
    def _dhash(path: str) -> tuple[int, float]:
        with Image.open(path) as image:
            aspect = image.width / max(1, image.height)
            gray = image.convert("L").resize((9, 8))
            pixels = list(gray.get_flattened_data())
        value = 0
        for row in range(8):
            for column in range(8):
                value = (value << 1) | int(
                    pixels[row * 9 + column] > pixels[row * 9 + column + 1]
                )
        return value, aspect

    @staticmethod
    def _decision(
        candidate: _Candidate,
        status: str,
        reason: str,
        duplicate_of: str | None = None,
    ) -> VisualSelectionDecision:
        return VisualSelectionDecision(
            item=candidate.item,
            status=status,
            reason=reason,
            duplicate_of=duplicate_of,
            priority=candidate.priority,
            required_for_review=candidate.required,
        )

    @staticmethod
    def _compose_pdf_text(pages: Iterable[PageExtraction]) -> str:
        sections = []
        for page in pages:
            body = page.text.strip()
            if page.simple_tables:
                body = "\n\n".join([body, *page.simple_tables]).strip()
            if body:
                sections.append(f"[PDF第{page.page}页]\n{body}")
        return "\n\n".join(sections)

    @staticmethod
    def _valid_blocks(markdown: str) -> bool:
        without_images = _IMAGE_PLACEHOLDER.sub("", markdown)
        without_markup = re.sub(r"[`#>*_|~\-]", "", without_images)
        return len(_MEANINGFUL.findall(without_markup)) >= 20

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"\s+", "", _CONTROL.sub("", text)).lower()

    @staticmethod
    def _clean_text(text: str) -> str:
        text = _CONTROL.sub("", text)
        return re.sub(r"[ \t]+", " ", text).strip()

    @staticmethod
    def _area_ratio(rect: fitz.Rect, page_area: float) -> float:
        return max(0.0, rect.width * rect.height / max(1.0, page_area))

    @staticmethod
    def _inside(rect: fitz.Rect, outer: fitz.Rect) -> bool:
        intersection = rect & outer
        return bool(rect.width and rect.height) and intersection.width * intersection.height >= rect.width * rect.height * 0.8

    @staticmethod
    def _overlap_ratio(left: fitz.Rect, right: fitz.Rect) -> float:
        intersection = left & right
        if intersection.is_empty:
            return 0.0
        return intersection.width * intersection.height / max(1.0, min(left.width * left.height, right.width * right.height))

    @staticmethod
    def _nearby_text(
        lines: list[tuple[str, fitz.Rect]], bbox: fitz.Rect
    ) -> str:
        expanded = bbox + (-36, -72, 36, 72)
        return " ".join(text for text, rect in lines if expanded.intersects(rect))[:500]
