from __future__ import annotations

import hashlib
import json
from zipfile import ZipFile

import fitz

from wiki_review_v2.fixtures import DocumentExtractor, FixtureDocumentSource


def test_drone_docx_fixture_preserves_blocks_pages_and_all_images(settings) -> None:
    case = settings.fixtures_root / "drone_hardware_rd"
    source = json.loads((case / "source_document.json").read_text(encoding="utf-8"))
    blocks = json.loads((case / "document_blocks.json").read_text(encoding="utf-8"))["blocks"]
    image_manifest = json.loads((case / "image_manifest.json").read_text(encoding="utf-8"))

    assert source["case_id"] == "drone_hardware_rd"
    assert source["title"] == "无人机硬件研发文档"
    assert len(blocks) == 84
    assert blocks[0]["block_type"] == "heading1"
    assert [block["image"]["token"] for block in blocks if block["block_type"] == "image"] == [
        "fixture_image_001",
        "fixture_image_002",
    ]

    pages = sorted((case / "pages").glob("page-*.png"))
    assert len(pages) == 7
    assert all(fitz.Pixmap(page).width > 0 for page in pages)
    assert not (case / "source.pdf").exists()

    with ZipFile(case / "source.docx") as archive:
        media_parts = sorted(name for name in archive.namelist() if name.startswith("word/media/"))
        assert media_parts == [item["source_part"] for item in image_manifest["images"]]
        for item in image_manifest["images"]:
            extracted = case / item["path"]
            assert extracted.read_bytes() == archive.read(item["source_part"])
            assert hashlib.sha256(extracted.read_bytes()).hexdigest() == item["sha256"]

    assert image_manifest["image_count"] == 2
    expected = json.loads((case / "expected_result.json").read_text(encoding="utf-8"))
    assert expected["result"] == "pass"
    assert expected["table_status"] == "AI通过待确认"
    bundle = FixtureDocumentSource().load(case)
    extracted = DocumentExtractor().extract(bundle.blocks)
    assert extracted["image_count"] == 2
    assert "无人机硬件研发文档" in extracted["content_markdown"]
    assert "部分内容可能由 AI 生成" in extracted["content_markdown"]


def test_drone_round1_need_revision_fixture_keeps_complete_visual_input(settings) -> None:
    original = settings.fixtures_root / "drone_hardware_rd"
    case = settings.fixtures_root / "drone_hardware_rd_round1_need_revision"
    original_source = json.loads((original / "source_document.json").read_text(encoding="utf-8"))
    source = json.loads((case / "source_document.json").read_text(encoding="utf-8"))
    mock = json.loads((case / "mock_llm_result.json").read_text(encoding="utf-8"))["response"]

    assert source["case_id"] == "drone_hardware_rd_round1_need_revision"
    assert source["document_id"] == original_source["document_id"]
    assert source["node_token"] == original_source["node_token"]
    assert source["review_round"] == 0
    assert source["status"] == "AI审稿中"

    bundle = FixtureDocumentSource().load(case)
    extracted = DocumentExtractor().extract(bundle.blocks)
    content = extracted["content_markdown"]
    assert "具体型号、主频、Flash/RAM容量" in content
    assert "动作阈值及选型计算尚未完成" in content
    assert "纹波上限和通过标准待补充" in content
    assert extracted["image_count"] == 2

    pages = sorted((case / "pages").glob("page-*.png"))
    assert len(pages) == 4
    assert all(fitz.Pixmap(page).width > 0 for page in pages)
    assert not (case / "source.pdf").exists()

    assert mock["result"] == "need_revision"
    assert mock["major_count"] == 3
    assert {issue["issue_id"] for issue in mock["issues"]} == {
        "drone-major-mcu-selection",
        "drone-major-power-design",
        "drone-major-verification-criteria",
    }
    assert mock["visual_evidence_assessment"]["coverage"] == "complete"


def test_drone_round2_fixture_resolves_real_review_issues(settings) -> None:
    round1 = settings.fixtures_root / "drone_hardware_rd_round1_need_revision"
    round2 = settings.fixtures_root / "drone_hardware_rd_round2_pass"
    round1_source = json.loads((round1 / "source_document.json").read_text(encoding="utf-8"))
    source = json.loads((round2 / "source_document.json").read_text(encoding="utf-8"))
    mock = json.loads((round2 / "mock_llm_result.json").read_text(encoding="utf-8"))["response"]
    round1_images = json.loads((round1 / "image_manifest.json").read_text(encoding="utf-8"))["images"]
    round2_images = json.loads((round2 / "image_manifest.json").read_text(encoding="utf-8"))["images"]

    assert source["document_id"] == round1_source["document_id"]
    assert source["node_token"] == round1_source["node_token"]
    assert source["status"] == "需修改"
    assert source["review_round"] == 1
    assert [item["issue_id"] for item in source["previous_issues"]] == ["major-1", "major-2", "minor-1"]
    assert [item["sha256"] for item in round2_images] == [item["sha256"] for item in round1_images]

    bundle = FixtureDocumentSource().load(round2)
    assert bundle.previous_review is not None
    assert [item.issue_id for item in bundle.previous_review.issues] == ["major-1", "major-2", "minor-1"]
    extracted = DocumentExtractor().extract(bundle.blocks)
    content = extracted["content_markdown"]
    assert "STM32F405VGT6" in content
    assert "6.1 设计目标指标（待样机验证）" in content
    assert "5V负载预算" in content
    assert "连续30分钟控制周期超时次数为0" in content
    assert "|（注：部分内容可能由 AI 生成）" not in content
    assert "技术判断由研发人员完成并负责核验" in content
    assert extracted["image_count"] == 2

    pages = sorted((round2 / "pages").glob("page-*.png"))
    assert len(pages) == 5
    assert all(fitz.Pixmap(page).width > 0 for page in pages)
    assert not (round2 / "source.pdf").exists()

    assert mock["result"] == "pass"
    assert mock["similarity_check"]["status"] == "not_applicable"
    assert {item["issue_id"] for item in mock["re_review_assessment"]["resolutions"]} == {
        "major-1",
        "major-2",
    }
