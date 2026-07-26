from __future__ import annotations

import argparse
import json
from pathlib import Path

import fitz


ROOT = Path(__file__).resolve().parents[1]


def dump(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def issue(issue_id: str, level: str, category: str, problem: str, suggestion: str) -> dict[str, object]:
    return {
        "issue_id": issue_id,
        "level": level,
        "category": category,
        "position": "全文",
        "problem": problem,
        "suggestion": suggestion,
        "evidence_ids": [],
    }


def review(
    result: str,
    *,
    issues: list[dict[str, object]] | None = None,
    similarity_status: str = "no_similar",
    similarity_decision: str = "keep_independent",
    summary: str = "文档达到科研知识库最低质量标准。",
    visual_pages: list[int] | None = None,
    rereview: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    issues = issues or []
    counts = {level: sum(item["level"] == level for item in issues) for level in ("blocking", "major", "minor")}
    next_action = {
        "pass": "admin_confirm",
        "need_revision": "author_revise",
        "recommend_human_review": "human_review",
        "incomplete_review": "human_recheck",
        "reject": "reject",
    }[result]
    if similarity_decision == "merge_required":
        next_action = "merge_with_existing"
    elif similarity_decision == "reject_independent_submission":
        next_action = "reject_independent_submission"
    return {
        "result": result,
        "summary": summary,
        "pass_reason": "内容清晰、证据充分且可复现。" if result == "pass" else "",
        "blocking_count": counts["blocking"],
        "major_count": counts["major"],
        "minor_count": counts["minor"],
        "issues": issues,
        "similarity_check": {
            "status": similarity_status,
            "decision": similarity_decision,
            "summary": "未发现明显相似文章。" if similarity_status == "no_similar" else "存在需要处理的相似内容。",
            "candidate_count": 0,
            "candidates_considered": [],
        },
        "learning_trace_assessment": "包含明确的操作过程、观察和结论。",
        "visual_evidence_assessment": {
            "coverage": "complete",
            "pages_reviewed": visual_pages or [1],
            "evidence_used": [],
            "limitations": [],
        },
        "revision_priority": [str(item["problem"]) for item in issues if item["level"] != "minor"],
        "suggested_next_action": next_action,
        "re_review_assessment": {"resolutions": rereview or []},
    }


def make_pdf(path: Path, page_texts: list[str]) -> None:
    doc = fitz.open()
    try:
        for text in page_texts:
            page = doc.new_page(width=595, height=842)
            page.insert_textbox(fitz.Rect(54, 54, 541, 788), text, fontname="china-s", fontsize=12, lineheight=1.4)
        doc.save(path)
    finally:
        doc.close()


def text_block(content: str, block_type: str = "text") -> dict[str, object]:
    return {block_type: {"elements": [{"text_run": {"content": content}}]}, "block_type": block_type}


def to_v1_blocks(blocks: list[dict[str, object]]) -> dict[str, object]:
    converted: list[dict[str, object]] = []
    image_index = 0
    for block in blocks:
        kind = str(block["type"])
        if kind == "heading":
            converted.append(text_block(str(block.get("text", "")), f"heading{int(block.get('level', 1))}"))
        elif kind == "code":
            item = text_block(str(block.get("text", "")), "code")
            item["code"]["language"] = str(block.get("language", ""))
            converted.append(item)
        elif kind == "table":
            lines = [line.strip() for line in str(block.get("markdown", block.get("text", ""))).splitlines() if line.strip()]
            rows = [
                [cell.strip() for cell in line.strip("|").split("|")]
                for line in lines
                if not all(set(cell.strip()) <= {"-", ":"} for cell in line.strip("|").split("|"))
            ]
            columns = max((len(row) for row in rows), default=0)
            children: list[dict[str, object]] = []
            for row_index, row in enumerate(rows):
                for column_index in range(columns):
                    children.append(
                        {
                            "block_type": "table_cell",
                            "table_cell": {"location": {"row": row_index, "column": column_index}},
                            "children": [text_block(row[column_index] if column_index < len(row) else "")],
                        }
                    )
            converted.append(
                {
                    "block_type": "table",
                    "table": {"column_size": columns},
                    "children": children,
                }
            )
        elif kind == "image":
            image_index += 1
            converted.append(
                {
                    "block_type": "image",
                    "image": {"token": f"fixture_image_{image_index:03d}"},
                }
            )
        else:
            converted.append(text_block(str(block.get("text", ""))))
    return {"blocks": converted}


def write_case(
    root: Path,
    case_id: str,
    *,
    title: str,
    blocks: list[dict[str, object]],
    response: dict[str, object],
    expected: str,
    source_extra: dict[str, object] | None = None,
    candidates: list[dict[str, object]] | None = None,
    previous: dict[str, object] | None = None,
    fake_extra: dict[str, object] | None = None,
    pdf_pages: list[str] | None = None,
    fixture_options: dict[str, object] | None = None,
) -> None:
    case = root / case_id
    case.mkdir(parents=True, exist_ok=False)
    source = {
        "case_id": case_id,
        "document_id": f"test_doc_{case_id}",
        "node_token": f"test_node_{case_id}",
        "title": title,
        "wiki_name": "离线测试知识库",
        "author_id": f"test_user_{case_id}",
        "author": f"测试用户_{case_id}",
        "link": f"https://example.invalid/wiki/test_doc_{case_id}",
        "review_method": "AI",
        "status": "AI审稿中",
        "review_round": 0,
        "updated_at": "2026/07/01 09:00:00",
        "last_ai_review_at": "",
    }
    source.update(source_extra or {})
    if previous:
        source["previous_issues"] = [
            {key: value for key, value in item.items() if key != "issue_id"}
            for item in previous.get("issues", [])
        ]
    dump(case / "source_document.json", source)
    dump(case / "document_blocks.json", to_v1_blocks(blocks))
    dump(case / "attachment_metadata.json", {"document_id": source["document_id"], "attachments": []})
    dump(
        case / "similarity_candidates.json",
        {"document_id": source["document_id"], "similar_documents": candidates or []},
    )
    extra = fake_extra or {}
    if "raw" in extra:
        mock = {"behavior": "raw", "raw_output": extra["raw"]}
    elif "raise" in extra:
        mock = {"behavior": "raise", "error_type": extra["raise"]}
    else:
        mock = {"behavior": "return", "response": response}
    if "visual_batches" in extra:
        mock["visual_batches"] = extra["visual_batches"]
    dump(case / "mock_llm_result.json", mock)
    review_round = int(source["review_round"]) + 1
    failed = expected == "failure"
    status_map = {
        "pass": "AI通过待确认",
        "need_revision": "需修改",
        "recommend_human_review": "待分配人工审稿",
        "incomplete_review": "待分配人工审稿",
        "reject": "已拒稿",
    }
    dump(
        case / "expected_result.json",
        {
            "exit_code": 1 if failed else 0,
            "result": None if failed else expected,
            "table_status": "" if failed else status_map[expected],
            "review_mode": "re_review" if int(source["review_round"]) > 0 else "initial_review",
            "review_round": review_round,
            "ocr_success": False,
            "table_count": sum(item["type"] == "table" for item in blocks),
            "failure_stage": ("model_output_parse" if "raw" in extra else "model_call") if failed else "",
            "notifications_generated": not failed,
            "modified_document_detected": int(source["review_round"]) > 0,
        },
    )
    if fixture_options:
        dump(case / "fixture_options.json", fixture_options)
    if pdf_pages is not None:
        make_pdf(case / "source.pdf", pdf_pages)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT / "fixtures")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()) and not args.force:
        raise SystemExit("Fixture 目录非空；如需重新生成请显式传入 --force。")
    if args.force:
        for child in root.iterdir():
            if child.is_dir():
                import shutil

                shutil.rmtree(child)

    base_blocks = [
        {"type": "heading", "text": "实验记录", "level": 1},
        {"type": "paragraph", "text": "本文记录依赖安装、实际命令、观察到的结果和故障排查过程。"},
        {"type": "code", "text": "python -m pytest -q\n# 12 passed", "language": "powershell"},
        {"type": "paragraph", "text": "结论：调整输入校验后，异常被稳定复现并修复。"},
    ]
    write_case(
        root,
        "basic_pass",
        title="笔记_Python基础实践-v1.0",
        blocks=base_blocks,
        response=review("pass"),
        expected="pass",
        source_extra={"author_id": "test_user_001", "author": "测试用户001"},
    )
    major = issue("issue-1", "major", "reproducibility", "缺少输入样例和关键输出。", "补充最小输入、命令与输出。")
    write_case(
        root,
        "need_revision",
        title="项目_Python日志分析练习-v1.0",
        blocks=[{"type": "paragraph", "text": "我们运行了脚本，效果不错。"}],
        response=review("need_revision", issues=[major], summary="已有实践痕迹，但无法复现。"),
        expected="need_revision",
        source_extra={"author_id": "test_user_002", "author": "测试用户002"},
    )
    blocking = issue("issue-ai", "blocking", "ai_generation_artifact", "正文保留了模型面向用户的回答前缀。", "删除 AI 回复残留并重写为个人实践记录。")
    write_case(
        root,
        "reject_ai_generated",
        title="笔记_通用编程概念整理-v1.0",
        blocks=[{"type": "paragraph", "text": "当然可以，下面是为你生成的完整文章。"}],
        response=review("reject", issues=[blocking], summary="存在明确的 AI 回复残留。"),
        expected="reject",
        source_extra={"author_id": "test_user_003", "author": "测试用户003"},
    )
    candidate = {
        "document_id": "existing-1",
        "title": "已有部署手册",
        "wiki_name": "工程知识库",
        "link": "fixture://existing-1",
        "status": "已公示",
        "score": 0.91,
        "content": "同一工具的完整部署步骤和故障处理。",
    }
    similarity_review = review("pass", similarity_status="merge_recommended", similarity_decision="merge_required")
    similarity_review["similarity_check"]["candidates_considered"] = [
        {
            "title": candidate["title"],
            "wiki_name": candidate["wiki_name"],
            "link": candidate["link"],
            "status": candidate["status"],
            "score": candidate["score"],
            "relationship": "duplicate",
            "evidence": "章节结构、部署步骤和故障处理高度重合。",
        }
    ]
    similarity_review["similarity_check"]["candidate_count"] = 1
    write_case(
        root,
        "similarity_merge",
        title="重复的部署步骤",
        blocks=base_blocks,
        response=similarity_review,
        expected="need_revision",
        candidates=[candidate, {**candidate, "document_id": "doc-similarity_merge", "score": 0.99}],
    )
    previous = {
        "schema_version": "2.0",
        "document_id": "doc-rereview_resolved",
        "review_round": 1,
        "result": "need_revision",
        "issues": [
            {
                "issue_id": "old-1",
                "level": "major",
                "category": "reproducibility",
                "problem": "缺少命令输出。",
                "suggestion": "补充输出。",
            },
            {
                "issue_id": "old-minor",
                "level": "minor",
                "category": "format",
                "problem": "空格问题。",
                "suggestion": "调整空格。",
            },
        ],
    }
    rereview = [{"issue_id": "old-1", "status": "resolved", "evidence": "正文新增命令和测试输出。"}]
    write_case(
        root,
        "rereview_resolved",
        title="复审已修正文档",
        blocks=base_blocks,
        response=review("pass", similarity_status="not_applicable", rereview=rereview),
        expected="pass",
        source_extra={
            "review_round": 1,
            "status": "需修改",
            "updated_at": "2026/07/02 10:00:00",
            "last_ai_review_at": "2026/07/01 10:00:00",
        },
        previous=previous,
        pdf_pages=["复审页面：命令 python -m pytest -q\n结果：12 passed"],
    )
    multimodal_blocks = base_blocks + [
        {"type": "table", "markdown": "| 配置 | 值 |\n|---|---|\n| timeout | 30 |"},
        {"type": "image", "caption": "终端截图显示测试全部通过"},
    ]
    multimodal_review = review("pass", visual_pages=[1, 2])
    multimodal_review["visual_evidence_assessment"]["evidence_used"] = [
        {
            "evidence_id": "page-2",
            "page": 2,
            "observation": "页面显示 pytest 命令及 12 passed 结果。",
            "supports": ["learning_trace", "reproducibility"],
        }
    ]
    write_case(
        root,
        "multimodal_pass",
        title="包含命令和表格的多模态记录",
        blocks=multimodal_blocks,
        response=multimodal_review,
        expected="pass",
        fixture_options={"pdf_required": True},
        pdf_pages=["实验配置表\ntimeout = 30", "命令：python -m pytest -q\n结果：12 passed"],
    )
    write_case(
        root,
        "partial_pages",
        title="部分页面不可用",
        blocks=base_blocks,
        response=review("pass", visual_pages=[1]),
        expected="incomplete_review",
        fixture_options={"pdf_required": True, "render_fail_pages": [2]},
        pdf_pages=["第一页可读", "第二页模拟渲染失败"],
    )
    long_response = review("pass", visual_pages=list(range(1, 17)))
    long_batches = [
        {
            "batch_summary": f"检查第 {start}-{min(start + 7, 16)} 页。",
            "pages_reviewed": list(range(start, min(start + 8, 17))),
            "evidence": [
                {
                    "evidence_id": f"page-{start}",
                    "page": start,
                    "observation": "页面包含实际命令和验证结果。",
                    "supports": ["reproducibility"],
                }
            ],
            "limitations": [],
        }
        for start in (1, 9)
    ]
    write_case(
        root,
        "long_pdf",
        title="长篇实验记录",
        blocks=base_blocks,
        response=long_response,
        expected="pass",
        fake_extra={"visual_batches": long_batches},
        pdf_pages=[f"第 {page} 页：命令、参数、日志和阶段结论。" for page in range(1, 17)],
    )
    human = review("recommend_human_review", summary="内容涉及需要领域专家判断的敏感结论。")
    write_case(
        root,
        "human_review",
        title="需要领域专家判断",
        blocks=base_blocks,
        response=human,
        expected="recommend_human_review",
    )
    write_case(
        root,
        "invalid_model_output",
        title="笔记_JSON解析测试-v1.0",
        blocks=base_blocks,
        response=review("pass"),
        expected="failure",
        fake_extra={"raw": "这不是 JSON"},
        source_extra={"author_id": "test_user_007", "author": "测试用户007"},
    )
    write_case(
        root,
        "api_failure",
        title="模型 API 故障案例",
        blocks=base_blocks,
        response=review("pass"),
        expected="failure",
        fake_extra={"raise": "api"},
    )
    print(f"generated {len(list(root.iterdir()))} fixtures in {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
