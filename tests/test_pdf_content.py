from __future__ import annotations

import json
from pathlib import Path

import fitz
from PIL import Image, ImageDraw

from wiki_review_v2.models import FixtureOptions, SourceDocument
from wiki_review_v2.pdf import PdfPageRenderer
from wiki_review_v2.pdf_content import PdfDocumentInputPreparer
from wiki_review_v2.runner import ReviewRunner


def _source() -> SourceDocument:
    return SourceDocument(
        case_id="pdf-test",
        document_id="pdf-test",
        node_token="pdf-test",
        title="PDF test",
        wiki_name="test",
        author_id="user",
        author="user",
        link="https://example.invalid/pdf-test",
    )


def _native_pdf(path: Path, pages: int = 13) -> None:
    document = fitz.open()
    for index in range(1, pages + 1):
        page = document.new_page(width=595, height=842)
        page.insert_text((54, 50), "REPEATED HEADER", fontsize=10)
        page.insert_textbox(
            fitz.Rect(54, 120, 541, 260),
            f"Page {index} native technical record with command parameters observations validation and conclusion.",
            fontsize=11,
        )
        page.insert_text((290, 810), str(index), fontsize=9)
    document.save(path)
    document.close()


def test_pdf_only_uses_page_tagged_native_text_and_removes_margins(settings, tmp_path: Path) -> None:
    case = tmp_path / "case"
    output = tmp_path / "output"
    case.mkdir()
    output.mkdir()
    _native_pdf(case / "source.pdf")
    preparer = PdfDocumentInputPreparer(settings, PdfPageRenderer(settings))

    prepared, extraction, selection, manifest, reason = preparer.prepare(
        case_path=case,
        output_dir=output,
        source=_source(),
        options=FixtureOptions(),
        blocks_content="",
    )

    assert reason == ""
    assert prepared.mode == "selective_regions"
    assert prepared.structured_content_source == "pdf"
    assert "[PDF第1页]" in prepared.structured_content
    assert "[PDF第13页]" in prepared.structured_content
    assert "REPEATED HEADER" not in prepared.structured_content
    assert extraction.text_pages_covered == 13
    assert selection.selected_count == 0
    assert manifest.rendered_pages == []


def test_reliable_blocks_remain_structured_content_without_pdf_duplication(settings, tmp_path: Path) -> None:
    case = tmp_path / "case"
    output = tmp_path / "output"
    case.mkdir()
    output.mkdir()
    _native_pdf(case / "source.pdf")
    blocks = "# Blocks title\n\nThis is reliable Blocks content with parameters STM32H743 and 3.3V."
    preparer = PdfDocumentInputPreparer(settings, PdfPageRenderer(settings))
    prepared, *_ = preparer.prepare(
        case_path=case,
        output_dir=output,
        source=_source(),
        options=FixtureOptions(),
        blocks_content=blocks,
    )
    assert prepared.structured_content_source == "blocks"
    assert prepared.structured_content == blocks
    assert "Page 13 native technical record" not in prepared.structured_content


def test_short_document_keeps_all_full_pages(settings, tmp_path: Path) -> None:
    case = tmp_path / "case"
    output = tmp_path / "output"
    case.mkdir()
    output.mkdir()
    _native_pdf(case / "source.pdf", pages=2)
    preparer = PdfDocumentInputPreparer(settings, PdfPageRenderer(settings))
    prepared, _, selection, _, _ = preparer.prepare(
        case_path=case,
        output_dir=output,
        source=_source(),
        options=FixtureOptions(),
        blocks_content="Reliable Blocks content contains enough useful technical characters for review.",
    )
    assert prepared.mode == "legacy_full_pages"
    assert [item.evidence_id for item in prepared.visual_items] == ["page-1", "page-2"]
    assert all(item.type == "full_page" for item in selection.selected)


def test_short_document_over_byte_limit_switches_to_selective(settings, tmp_path: Path) -> None:
    case = tmp_path / "case"
    output = tmp_path / "output"
    case.mkdir()
    output.mkdir()
    _native_pdf(case / "source.pdf", pages=2)
    limited = settings.model_copy(update={"direct_image_bytes_limit": 1024})
    prepared, *_ = PdfDocumentInputPreparer(limited, PdfPageRenderer(limited)).prepare(
        case_path=case,
        output_dir=output,
        source=_source(),
        options=FixtureOptions(),
        blocks_content="Reliable Blocks content contains enough useful technical characters for review.",
    )
    assert prepared.mode == "selective_regions"


def test_dhash_matches_light_resize_but_not_unrelated(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    resized = tmp_path / "resized.png"
    other = tmp_path / "other.png"
    image = Image.new("RGB", (160, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 70, 100), fill="black")
    draw.ellipse((90, 20, 145, 90), fill="blue")
    image.save(first)
    image.resize((320, 240)).save(resized)
    unrelated = Image.new("RGB", (160, 120), "white")
    ImageDraw.Draw(unrelated).polygon([(20, 100), (80, 10), (145, 100)], fill="red")
    unrelated.save(other)
    first_hash, _ = PdfDocumentInputPreparer._dhash(str(first))
    resized_hash, _ = PdfDocumentInputPreparer._dhash(str(resized))
    other_hash, _ = PdfDocumentInputPreparer._dhash(str(other))
    assert (first_hash ^ resized_hash).bit_count() <= 5
    assert (first_hash ^ other_hash).bit_count() > 5


def test_visual_budget_omission_forces_incomplete(settings) -> None:
    limited = settings.model_copy(
        update={"max_visual_regions": 1, "max_full_page_fallbacks": 0}
    )
    summary = ReviewRunner(limited).run_case("long_pdf_mixed")
    assert summary.ok and summary.result == "incomplete_review"
    selection = json.loads(
        (summary.output_dir / "visual_selection.json").read_text(encoding="utf-8")
    )
    coverage = json.loads(
        (summary.output_dir / "input_coverage.json").read_text(encoding="utf-8")
    )
    assert selection["budget_exceeded"] is True
    assert selection["required_visuals_omitted"] is True
    assert coverage["visual_pages_complete"] is False
    assert coverage["missing_sources"]

