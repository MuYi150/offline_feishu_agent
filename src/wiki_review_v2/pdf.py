from __future__ import annotations

import shutil
from pathlib import Path

import fitz

from .config import Settings
from .errors import PdfError
from .models import FixtureOptions, SourceDocument, VisualManifest, VisualPage
from .storage import sha256_file


class PdfPageRenderer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def prepare(
        self,
        case_path: Path,
        output_dir: Path,
        source: SourceDocument,
        options: FixtureOptions,
        content: str,
    ) -> tuple[VisualManifest, str]:
        pages_source = case_path / "pages"
        conventional_pdf = case_path / "source.pdf"
        configured_pdf = options.source_pdf
        if pages_source.is_dir() and not configured_pdf and not conventional_pdf.exists():
            return self._copy_fixture_pages(pages_source, output_dir, options), ""

        if options.pdf_required and not configured_pdf and not conventional_pdf.exists():
            return self._unavailable("Fixture 要求提供 PDF，但未声明 source_pdf"), "要求的 PDF 未提供"

        if configured_pdf or conventional_pdf.exists():
            pdf_name = configured_pdf or "source.pdf"
            pdf_path = case_path / pdf_name
            if not pdf_path.exists():
                return self._unavailable(f"声明的 PDF 不存在：{pdf_name}"), "声明的 PDF 不存在"
            if pdf_path.stat().st_size > self.settings.max_pdf_bytes:
                return self._unavailable("PDF 文件大小超过安全上限"), "PDF 文件大小超过安全上限"
            kind = "pdf"
        else:
            if not content.strip():
                return self._unavailable("结构化正文为空，无法生成 PDF"), "结构化正文和 PDF 均不可用"
            pdf_path = output_dir / "generated_source.pdf"
            self.generate_pdf(content, pdf_path)
            kind = "generated_pdf"

        try:
            manifest = self.render_pdf(pdf_path, output_dir / "pages", options.render_fail_pages, kind)
            reason = "" if manifest.rendered_pages else "PDF 没有可用页面"
            return manifest, reason
        except PdfError as exc:
            return self._unavailable(exc.safe_message), exc.safe_message

    def generate_pdf(self, content: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = fitz.open()
        try:
            chunks = [content[i : i + 1200] for i in range(0, len(content), 1200)] or [""]
            for chunk in chunks:
                page = doc.new_page(width=595, height=842)
                page.insert_textbox(
                    fitz.Rect(54, 54, 541, 788),
                    chunk,
                    fontname="china-s",
                    fontsize=10,
                    lineheight=1.35,
                )
            doc.save(path)
        except Exception as exc:
            raise PdfError("无法从结构化正文生成 PDF") from exc
        finally:
            doc.close()

    def render_pdf(
        self, pdf_path: Path, pages_dir: Path, failed_pages: list[int], source_kind: str = "pdf"
    ) -> VisualManifest:
        try:
            document = fitz.open(pdf_path)
        except Exception as exc:
            raise PdfError("PDF 损坏或无法打开") from exc
        try:
            total = document.page_count
            if total <= 0:
                raise PdfError("PDF 不包含页面")
            if total > self.settings.max_pdf_pages:
                return VisualManifest(
                    source=source_kind,
                    total_pages=total,
                    failed_pages=list(range(self.settings.max_pdf_pages + 1, total + 1)),
                    limitations=[f"PDF 共 {total} 页，超过上限 {self.settings.max_pdf_pages}，未静默截断。"],
                )
            pages_dir.mkdir(parents=True, exist_ok=True)
            rendered: list[VisualPage] = []
            failed: list[int] = []
            zoom = self.settings.pdf_render_dpi / 72.0
            for index in range(total):
                page_no = index + 1
                if page_no in failed_pages:
                    failed.append(page_no)
                    continue
                try:
                    pix = document.load_page(index).get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                    if max(pix.width, pix.height) > self.settings.max_image_long_edge:
                        ratio = self.settings.max_image_long_edge / max(pix.width, pix.height)
                        pix = document.load_page(index).get_pixmap(
                            matrix=fitz.Matrix(zoom * ratio, zoom * ratio), alpha=False
                        )
                    target = pages_dir / f"page-{page_no:04d}.png"
                    pix.save(target)
                    rendered.append(self._page(target, page_no, pix.width, pix.height))
                except Exception:
                    failed.append(page_no)
            limitations = [f"页面渲染失败：{failed}"] if failed else []
            return VisualManifest(
                source=source_kind,
                total_pages=total,
                rendered_pages=rendered,
                failed_pages=failed,
                limitations=limitations,
            )
        finally:
            document.close()

    def _copy_fixture_pages(self, source_dir: Path, output_dir: Path, options: FixtureOptions) -> VisualManifest:
        files = sorted(path for path in source_dir.iterdir() if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
        if len(files) > self.settings.max_pdf_pages:
            return self._unavailable(f"页面数 {len(files)} 超过上限 {self.settings.max_pdf_pages}")
        target_dir = output_dir / "pages"
        target_dir.mkdir(parents=True, exist_ok=True)
        pages: list[VisualPage] = []
        failed: list[int] = []
        for index, item in enumerate(files, 1):
            if index in options.render_fail_pages:
                failed.append(index)
                continue
            target = target_dir / f"page-{index:04d}{item.suffix.lower()}"
            try:
                shutil.copy2(item, target)
                pix = fitz.Pixmap(target)
                pages.append(self._page(target, index, pix.width, pix.height))
            except Exception:
                failed.append(index)
        return VisualManifest(
            source="fixture_pages",
            total_pages=len(files),
            rendered_pages=pages,
            failed_pages=failed,
            limitations=[f"页面读取失败：{failed}"] if failed else [],
        )

    @staticmethod
    def _page(path: Path, page_no: int, width: int, height: int) -> VisualPage:
        return VisualPage(
            evidence_id=f"page-{page_no}",
            page=page_no,
            path=str(path.resolve()),
            width=width,
            height=height,
            byte_size=path.stat().st_size,
            sha256=sha256_file(path),
        )

    @staticmethod
    def _unavailable(reason: str) -> VisualManifest:
        return VisualManifest(source="unavailable", limitations=[reason])


def calculate_input_coverage(
    content: str,
    manifest: VisualManifest,
    *,
    attachments: list[dict[str, object]],
    attachment_content_required: bool,
    max_text_chars: int,
) -> tuple[dict[str, object], str]:
    limitations = list(manifest.limitations)
    missing: list[str] = []
    truncated = len(content) > max_text_chars
    if truncated:
        limitations.append(f"结构化正文 {len(content)} 字符，超过上限 {max_text_chars}，未提交模型。")
        missing.append("structured_text_over_limit")
    if not manifest.rendered_pages:
        missing.append("visual_pages")
    if attachment_content_required and attachments:
        missing.append("attachment_content")
        limitations.append("附件仅提供元数据，本阶段未打开附件内容。")
    coverage = {
        "schema_version": "2.0",
        "structured_text_available": bool(content.strip()),
        "visual_pages_complete": bool(manifest.total_pages) and not manifest.failed_pages and len(manifest.rendered_pages) == manifest.total_pages,
        "attachments_opened": False,
        "input_truncated": truncated,
        "missing_sources": missing,
        "limitations": limitations,
    }
    incomplete = ""
    if truncated:
        incomplete = "结构化正文超过安全上限"
    elif not content.strip() and not manifest.rendered_pages:
        incomplete = "结构化正文和视觉页面均不可用"
    elif manifest.total_pages > 0 and not manifest.rendered_pages:
        incomplete = "视觉页面不可用"
    return coverage, incomplete
