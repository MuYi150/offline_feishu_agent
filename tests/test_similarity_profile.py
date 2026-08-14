from __future__ import annotations

from pathlib import Path

import fitz

from wiki_review_v2.models import FixtureOptions, SourceDocument
from wiki_review_v2.similarity_profile import SimilarityProfileBuilder


def _source(document_id: str = "profile-doc") -> SourceDocument:
    return SourceDocument(
        case_id="profile-case",
        document_id=document_id,
        node_token="node",
        title="设计_无人机飞控测试-v1.0",
        wiki_name="测试知识库",
        author_id="author",
        author="测试者",
        link="https://example.invalid/profile",
    )


def _write_pdf(path: Path, text: str) -> None:
    with fitz.open() as document:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(54, 54, 541, 788), text, fontsize=11)
        document.save(path)


def test_valid_blocks_take_priority_over_native_pdf(settings, tmp_path: Path) -> None:
    _write_pdf(tmp_path / "source.pdf", "PDF_ONLY_MARKER with unrelated database deployment content.")
    markdown = (
        "# 飞控验证\nSTM32H743 通过 CAN-FD 连接执行器，并使用 24 V 电源完成台架测试。"
        "\n测试过程记录了参数、日志和最终结论。"
    )
    profile = SimilarityProfileBuilder(settings).build(
        source=_source(),
        extracted_content={"content_markdown": markdown},
        case_path=tmp_path,
        options=FixtureOptions(),
    )
    assert profile.source == "blocks"
    assert "STM32H743" in profile.content
    assert "PDF_ONLY_MARKER" not in profile.content


def test_empty_blocks_fall_back_to_native_pdf_text(settings, tmp_path: Path) -> None:
    _write_pdf(
        tmp_path / "source.pdf",
        "Native PDF flight controller uses STM32H743 and CAN-FD with 24 V power. "
        "Bench validation records configuration and test results.",
    )
    profile = SimilarityProfileBuilder(settings).build(
        source=_source(),
        extracted_content={"content_markdown": ""},
        case_path=tmp_path,
        options=FixtureOptions(),
    )
    assert profile.source == "pdf"
    assert "STM32H743" in profile.content
    assert profile.source_character_count > 20


def test_png_and_image_placeholders_are_never_used(settings, tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page-0001.png").write_bytes(b"PNG contains imaginary secret text")
    profile = SimilarityProfileBuilder(settings).build(
        source=_source(),
        extracted_content={"content_markdown": "[图片说明：STM32H743 图片]"},
        case_path=tmp_path,
        options=FixtureOptions(),
    )
    assert profile.source == "unavailable"
    assert profile.content == ""
    assert "STM32H743" not in profile.technical_entities


def test_query_is_readable_deduplicated_and_respects_distributed_limit(settings, tmp_path: Path) -> None:
    limited = settings.model_copy(update={"similarity_query_max_chars": 1000})
    repeated = "执行电源测试并记录结果。"
    markdown = (
        "# 电源验证\n"
        + "\n".join([repeated, repeated, repeated])
        + "\nSTM32H743 使用 CAN-FD 通信，输入 24 V，输出 5 V 3 A。"
        + "\n完成振动测试、温升测试和日志分析后形成结论。" * 8
    )
    profile = SimilarityProfileBuilder(limited).build(
        source=_source(),
        extracted_content={"content_markdown": markdown},
        case_path=tmp_path,
        options=FixtureOptions(),
    )
    assert len(profile.query_text) <= 1000
    assert profile.query_character_count == len(profile.query_text)
    assert profile.content.count(repeated) == 1
    assert "标题：" in profile.content and "正文" in profile.content
    assert "STM32H743" in profile.technical_entities
    assert any("24 V" in item for item in profile.parameters)
    assert all(len(item) >= 2 for item in profile.keywords)
    assert len(profile.source_content_hash) == 64


def test_long_query_samples_across_the_document(settings, tmp_path: Path) -> None:
    limited = settings.model_copy(update={"similarity_query_max_chars": 1000})
    markdown = "# 全文取样\n" + "\n".join(
        f"第{index}段记录飞控硬件测试、参数和结果，标记 SEGMENT_{index:03d}。"
        for index in range(100)
    )
    profile = SimilarityProfileBuilder(limited).build(
        source=_source(),
        extracted_content={"content_markdown": markdown},
        case_path=tmp_path,
        options=FixtureOptions(),
    )
    assert profile.query_truncated
    assert "SEGMENT_000" in profile.query_text
    assert any(f"SEGMENT_{index:03d}" in profile.query_text for index in range(80, 100))
    assert "similarity_query_truncated" in profile.limitations
