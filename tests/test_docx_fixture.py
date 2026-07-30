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
