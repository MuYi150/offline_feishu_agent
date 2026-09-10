from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import fitz
import httpx
import pytest

from wiki_review_v2.fixtures import FixtureDocumentSource, DocumentExtractor
from wiki_review_v2.model import FakeReviewModel
from wiki_review_v2.online.config import FeishuSettings
from wiki_review_v2.online.feishu import FeishuGateway, FeishuError
from wiki_review_v2.online.migration import migrate
from wiki_review_v2.online.records import COLUMNS, rows_to_records, expected_status
from wiki_review_v2.online.service import OnlineService
from wiki_review_v2.online.snapshots import BlocksAdapter, SnapshotError
from wiki_review_v2.online.state import OnlineHistoryStore, StateStore


VERSION = "2026/01/01 10:00:00"
NEXT = "2099/01/01 10:00:00"
CONFIG_HEADER = ["知识库名称", "是否启用同步", "待审核目录链接", "待审核目录 node_token"]


def row(node="node00000001", obj="obj00000001", *, status="AI审稿中", method="AI", rounds=0):
    return [1, "Python 日志分析实践", {"text": "Python 日志分析实践", "link": f"https://example.feishu.cn/wiki/{node}", "type": "url"},
            "测试库", "@作者", method, "", status, rounds, VERSION, "", "", node, obj]


class MockFeishu:
    def __init__(self, rows=None):
        self.rows = [COLUMNS[:], *(rows if rows is not None else [row()])]
        self.config = [CONFIG_HEADER, ["测试库", True, "https://example.feishu.cn/wiki/parent00000001", "parent00000001"]]
        self.nodes = {r[12]: {"node_token": r[12], "obj_token": r[13], "obj_type": "docx", "edit_time": r[9],
                             "title": r[1], "owner": "ou_author", "space_id": "space", "parent_node_token": "parent00000001"} for r in self.rows[1:]}
        self.members = list(self.nodes)
        self.writes, self.messages = [], []
        self.fail_sheet = False
        self.fail_messages = False
        self.after_write = None
        self.after_pdf = None
        self.blocks_calls = 0

    def read_sheet(self, kind):
        return deepcopy(self.rows if kind == "review" else self.config)

    def node(self, node):
        if node == "parent00000001":
            return {"node_token": node, "space_id": "space"}
        return deepcopy(self.nodes[node])

    def children(self, parent):
        return [deepcopy(self.nodes[n]) for n in self.members]

    def user(self, owner):
        return {"open_id": "ou_author", "name": "作者"}

    def update_cells(self, kind, n, updates):
        if self.fail_sheet and "K" in updates:
            raise FeishuError("sheet_failed")
        self.writes.append((kind, n, deepcopy(updates)))
        target = self.rows if kind == "review" else self.config
        for col, value in updates.items():
            target[n-1][ord(col)-65] = value
        if "K" in updates and self.after_write:
            self.after_write()

    def repair_format(self, n, template=None):
        self.writes.append(("format", n, template))

    def insert_row(self, n, values):
        assert n == len(self.rows) + 1
        self.rows.append(deepcopy(values))
        self.writes.append(("insert", n))

    def blocks(self, obj):
        self.blocks_calls += 1
        content = "本文记录使用 Python 进行日志分析的练习思路，包括日志读取、信息筛选和结果输出。我们运行 Python 脚本读取日志，检查输入输出，并记录实际验证过程。" * 8
        return [{"block_id": obj, "block_type": 1, "children": ["paragraph"]},
                {"block_id": "paragraph", "parent_id": obj, "block_type": 2,
                 "text": {"elements": [{"text_run": {"content": content}}]}}]

    def export_pdf(self, obj, max_bytes):
        with fitz.open() as pdf:
            page = pdf.new_page()
            page.insert_text((72, 72), "Python log analysis: input, processing and output verification.")
            data = pdf.tobytes()
        if self.after_pdf:
            self.after_pdf()
        return data

    def send_message(self, recipient, content, key):
        self.messages.append((recipient, content, key))
        if self.fail_messages:
            raise FeishuError("send_timeout", uncertain=True)
        return "message-" + key


@pytest.fixture
def online(settings, tmp_path):
    api = MockFeishu()
    root = tmp_path / "online"
    settings = settings.model_copy(update={"output_root": root / "outputs"})
    response = FixtureDocumentSource().load(settings.fixtures_root / "need_revision").fake_model_response
    model = FakeReviewModel(response)
    service = OnlineService(api, FeishuSettings(domain="example.feishu.cn", admin_open_id="ou_admin"), settings, root, model_override=model)
    return service, api, model


def jobs(service):
    return [v for _, v in service.state.items("job")]


def test_dry_run_no_changes_or_state(online):
    service, api, _ = online
    service.sync()
    assert not api.writes and not api.messages and not service.root.exists()


@pytest.mark.parametrize("write", [False, True])
def test_missing_node_cell_backfilled_from_link(online, write):
    service, api, model = online
    expected_node = api.rows[1][12]
    api.rows[1][12] = ""
    records = service.sync(write=write)
    assert records[0]["row"][12] == expected_node
    assert any(e["action"] == "update" and "M" in e["columns"] for e in service.events)
    assert api.rows[1][12] == (expected_node if write else "")
    assert len(api.rows) == 2 and model.call_count == 0
    if write:
        service.events.clear()
        service.sync(write=True)
        assert not any(e["action"] == "update" and "M" in e["columns"] for e in service.events)


def test_capture_real_blocks_pdf_consumed_and_retry_no_model(online):
    service, api, model = online
    service.sync(write=True)
    assert len(jobs(service)) == 1, service.events
    fixture = FixtureDocumentSource().load(Path(jobs(service)[0]["snapshot"]))
    assert fixture.source_document.document_id == api.rows[1][13]
    assert fixture.fixture_options.pdf_required
    api.fail_sheet = True
    service.review(write=True)
    assert jobs(service)[0]["status"] == "sheet_pending", service.events
    assert api.rows[1][8] == 0 and not service.state.items("history")
    calls = model.call_count
    api.fail_sheet = False
    service.retry(write=True)
    assert jobs(service)[0]["status"] == "committed", service.events
    assert api.rows[1][7:9] == ["需修改", 1]
    assert len(api.messages) == 3  # Two result messages and the delivery-failure admin alert.
    assert len(service.state.items("history")) == 1
    service.sync(write=True)
    service.review(write=True)
    service.retry(write=True)
    assert model.call_count == calls and len(api.messages) == 3


def test_new_renamed_and_changed_tokens_no_duplicate(online):
    service, api, _ = online
    api.rows = [COLUMNS[:]]
    service.sync(write=True)
    assert len(api.rows) == 2 and api.rows[1][7] == "已投稿"
    api.nodes[api.members[0]].update(title="改名", obj_token="obj00000002")
    service.sync(write=True)
    assert len(api.rows) == 2 and api.rows[1][1] == "改名" and api.rows[1][13] == "obj00000002"
    assert not jobs(service)


def test_capture_change_discards_snapshot_no_round(online):
    service, api, model = online
    api.after_pdf = lambda: api.nodes[api.members[0]].update(edit_time=NEXT)
    service.sync(write=True)
    assert not jobs(service) and api.rows[1][8] == 0 and model.call_count == 0
    assert not list((service.root / "snapshots").glob("[!.]*"))


@pytest.mark.parametrize("change", ["version", "method", "public", "moved"])
def test_stale_before_review_never_calls_model(online, change):
    service, api, model = online
    service.sync(write=True)
    if change == "version": api.nodes[api.members[0]]["edit_time"] = NEXT
    if change == "method": api.rows[1][5] = "人工"
    if change == "public": api.rows[1][11] = NEXT
    if change == "moved": api.members = []
    service.review(write=True)
    assert model.call_count == 0 and jobs(service)[0]["status"] == "obsolete", service.events


def test_row_moves_and_crash_after_sheet_write(online):
    service, api, model = online
    service.sync(write=True)
    api.rows.insert(1, row("node00000002", "obj00000002", method="人工"))
    def crash():
        raise RuntimeError("interrupted after successful remote write")
    api.after_write = crash
    service.review(write=True)
    assert jobs(service)[0]["status"] == "sheet_pending", service.events
    assert api.rows[1][8] == 0 and api.rows[2][8] == 1
    calls = model.call_count
    api.after_write = None
    service.retry(write=True)
    assert jobs(service)[0]["status"] == "committed", service.events
    assert model.call_count == calls and len(service.state.items("history")) == 1
    assert len([w for w in api.writes if len(w) == 3 and isinstance(w[2], dict) and "K" in w[2]]) == 1


def test_uncertain_messages_not_automatically_resent(online):
    service, api, model = online
    service.sync(write=True)
    service.review(write=True)
    api.fail_messages = True
    service.retry(write=True)
    original = [x for x in api.messages if x[0] == "ou_author"]
    service.retry(write=True)
    assert len(original) == 1 and len([x for x in api.messages if x[0] == "ou_author"]) == 1
    assert any(v["status"] == "uncertain" for _, v in service.state.items("delivery"))
    assert api.rows[1][8] == 1 and service.state.items("history")


def test_review_failure_resume_uses_checkpoint(online):
    service, api, model = online
    model.fixture_response["raise"] = "api"
    service.sync(write=True)
    service.review(write=True)
    assert jobs(service)[0]["status"] == "review_failed", service.events
    assert api.rows[1][8] == 0 and not service.state.items("history")
    count = model.call_count
    model.fixture_response.pop("raise")
    service.review(write=True)
    assert jobs(service)[0]["status"] == "committed", service.events
    assert model.call_count == count + 1


def test_publication_only_then_invalidate(online):
    service, api, model = online
    service.sync(write=True)
    service.review(write=True)
    assert not service.index_store.query(exclude_document_id="other")
    api.rows[1][11] = VERSION
    calls = model.call_count
    service.index(write=True)
    articles = service.index_store.query(exclude_document_id="other")
    assert len(articles) == 1, service.events
    assert articles[0].review_result == "" and model.call_count == calls
    api.nodes[api.members[0]]["edit_time"] = NEXT
    service.index(write=True)
    assert not service.index_store.query(exclude_document_id="other")


def test_historical_publication_index_without_review(online):
    service, api, model = online
    api.rows[1][11] = VERSION
    service.index(write=True)
    assert len(service.index_store.query(exclude_document_id="other")) == 1, service.events
    assert not jobs(service) and not api.messages and api.rows[1][8] == 0


def test_manual_public_missing_and_failed_scan(online):
    service, api, _ = online
    api.rows[1][5] = "人工+AI"
    service.sync(write=True)
    assert api.rows[1][7] == "待分配人工审稿" and not jobs(service)
    api.rows[1][11] = VERSION
    api.members = []
    service.sync(write=True)
    assert api.rows[1][7] == "已公示"
    api.rows[1][11] = ""
    api.rows[1][7] = "人工审稿中"
    api.children = lambda parent: (_ for _ in ()).throw(FeishuError("scan_failed"))
    service.sync(write=True)
    assert api.rows[1][7] != "已移出待审核"


def test_missing_history_and_migration(online, tmp_path):
    service, api, _ = online
    api.rows[1][8] = 1
    api.rows[1][10] = "2025/01/01 10:00:00"
    api.rows[1][7] = "已修改待复审"
    service.sync(write=True)
    assert not jobs(service)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    old = {api.members[0]: {"last_review_round": 1, "last_review_time": api.rows[1][10], "last_result": "need_revision",
        "last_issues": [{"level": "major", "category": "content", "problem": "缺少输入", "suggestion": "补充输入"}]}}
    path = legacy / "ai_review_history.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    original = path.read_bytes()
    preview = migrate(service, legacy)
    assert preview["migrated"] == api.members and not service.state.items("history")
    migrate(service, legacy, write=True)
    migrate(service, legacy, write=True)
    history = OnlineHistoryStore(service.state, api.members[0], 0)
    assert history.record_count(api.rows[1][13]) == 1
    assert history.load_latest(api.rows[1][13]).result.issues[0].issue_id.startswith("v1-")
    service.sync(write=True)
    assert len(jobs(service)) == 1, service.events
    assert path.read_bytes() == original


def test_third_round_handoff_and_rejected_new_cycle(online):
    service, api, model = online
    api.rows[1][8:11] = [2, VERSION, "2025/01/01 10:00:00"]
    api.rows[1][7] = "已修改待复审"
    history = OnlineHistoryStore(service.state, api.members[0], 0)
    previous = model.fixture_response["review"]["issues"][0]
    history.append(api.rows[1][13], {"legacy": True, "run_id": "previous", "review_round": 2,
                                   "result": "need_revision", "issues": [previous]})
    model.fixture_response["review"]["re_review_assessment"] = {"resolutions": [
        {"issue_id": "issue-1", "status": "unresolved", "evidence": "仍缺少输入样例"}]}
    service.sync(write=True)
    service.review(write=True)
    assert api.rows[1][7:9] == ["待分配人工审稿", 3], service.events
    assert service.state.control(api.members[0])["handoff"]
    calls = model.call_count
    api.nodes[api.members[0]]["edit_time"] = NEXT
    service.sync(write=True)
    service.review(write=True)
    assert model.call_count == calls
    api.rows[1][7] = "已拒稿"
    service.sync(write=True)
    assert api.rows[1][8] == 0 and service.state.control(api.members[0])["cycle"] == 1


def test_blocks_nested_table_sheet_and_missing_child():
    gateway = MockFeishu()
    gateway.read_values = lambda *args: [["嵌入表头", "值"], ["Python", "1"]]
    raw = [{"block_id": "doc", "block_type": 1, "children": ["t", "sheet", "image", "file"]},
           {"block_id": "t", "block_type": 31, "table": {"cells": ["cell"], "property": {"column_size": 1}}},
           {"block_id": "cell", "parent_id": "t", "block_type": 32, "children": ["p"]},
           {"block_id": "p", "parent_id": "cell", "block_type": 2, "text": {"elements": [{"text_run": {"content": "单元格内容"}}]}},
           {"block_id": "sheet", "block_type": 30, "sheet": {"token": "spreadsheet_sheetid"}},
           {"block_id": "image", "block_type": 27, "image": {"token": "img"}},
           {"block_id": "file", "block_type": 23, "file": {"name": "test.zip", "token": "file"}}]
    blocks, attachments = BlocksAdapter(gateway).adapt(raw, "doc")
    markdown = DocumentExtractor().extract(blocks)["content_markdown"]
    assert "单元格内容" in markdown and "嵌入表头" in markdown
    assert attachments[0]["name"] == "test.zip"
    with pytest.raises(SnapshotError):
        BlocksAdapter(gateway).adapt(raw[:-1], "doc")


def test_gateway_pagination_and_uncertain_send():
    seen = []
    def handler(req):
        seen.append(req)
        if "auth" in req.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "fake"})
        if req.url.path.endswith("/messages"):
            raise httpx.ReadTimeout("timeout")
        if req.url.params.get("page_token"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [{"node_token": "b"}], "has_more": False}})
        return httpx.Response(200, json={"code": 0, "data": {"items": [{"node_token": "a"}], "has_more": True, "page_token": "next"}})
    gateway = FeishuGateway(FeishuSettings(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert [n["node_token"] for n in gateway.children({"node_token": "root", "space_id": "space"})] == ["a", "b"]
    with pytest.raises(FeishuError) as err:
        gateway.send_message("ou_author", "test", "stable")
    assert err.value.uncertain and len([r for r in seen if r.url.path.endswith("/messages")]) == 1


def test_run_lock(tmp_path):
    state = StateStore(tmp_path)
    with state.lock():
        with pytest.raises(RuntimeError):
            with StateStore(tmp_path).lock():
                pytest.fail("overlapping lock acquired")


def test_modified_during_model_keeps_artifacts_without_delivery(online):
    service, api, model = online
    invoke = model.invoke
    def mutate(request):
        response = invoke(request)
        if request.phase == "final_review":
            api.nodes[api.members[0]]["edit_time"] = NEXT
        return response
    model.invoke = mutate
    service.sync(write=True)
    service.review(write=True)
    job = jobs(service)[0]
    assert job["status"] == "obsolete", service.events
    assert (Path(job["run_dir"]) / "parsed_review_result.json").exists()
    assert not service.state.items("history") and api.rows[1][8] == 0
    assert not any(v.get("job_id") for _, v in service.state.items("delivery"))


def test_resume_completed_graph_without_calling_model_again(online, monkeypatch):
    service, api, model = online
    service.sync(write=True)
    put = service.state.put
    def interrupted(kind, key, value):
        if kind == "job" and value["status"] == "reviewed":
            raise RuntimeError("crash between graph and journal")
        return put(kind, key, value)
    monkeypatch.setattr(service.state, "put", interrupted)
    service.review(write=True)
    assert jobs(service)[0]["status"] == "reviewing"
    calls = model.call_count
    monkeypatch.setattr(service.state, "put", put)
    service.review(write=True)
    assert jobs(service)[0]["status"] == "committed", service.events
    assert model.call_count == calls


def test_resume_preflight_failure_without_checkpoint(online):
    service, api, model = online
    service.sync(write=True)
    service.model_override = None
    service.settings = service.settings.model_copy(update={"kimi_api_key": None})
    service.review(write=True, real_model=True)
    assert jobs(service)[0]["status"] == "review_failed", service.events
    service.model_override = model
    service.review(write=True)
    assert jobs(service)[0]["status"] == "committed", service.events


def test_partial_notification_retry_preserves_sent_parts(online):
    service, api, model = online
    model.fixture_response["review"]["summary"] = "审稿概述。" * 4000
    service.sync(write=True)
    service.review(write=True)
    author_parts = [v for _, v in service.state.items("delivery") if v["role"] == "author"]
    assert len(author_parts) > 1
    send = api.send_message
    failed = set()
    def fail_second_once(recipient, content, key):
        if key == author_parts[1]["id"] and not failed:
            failed.add(key)
            raise FeishuError("rate_limit")
        return send(recipient, content, key)
    api.send_message = fail_second_once
    service.retry(write=True)
    assert service.state.get("delivery", author_parts[0]["id"])["status"] == "sent"
    assert service.state.get("delivery", author_parts[1]["id"])["status"] == "failed"
    service.retry(write=True)
    ids = [m[2] for m in api.messages if m[0] == "ou_author"]
    assert len(ids) == len(set(ids)) == len(author_parts)


def test_preview_isolated_and_cli_requires_both_authorizations(online):
    from wiki_review_v2.online.__main__ import execute, parser
    service, api, model = online
    service.preview(limit=1)
    assert not service.state.items("job") and not service.state.items("history") and not api.writes and not api.messages
    assert list((service.root / "previews").glob("*/outputs/*/*/parsed_review_result.json")), service.events
    assert not service.settings.similarity_index_path.exists()
    service.model_override = None
    with pytest.raises(ValueError, match="real-model"):
        execute(service, parser().parse_args(["review", "--write"]))
    with pytest.raises(ValueError, match="extract-only"):
        execute(service, parser().parse_args(["run", "--extract-only", "--write"]))


def test_bitable_and_embedded_change_validation():
    gateway = MockFeishu()
    gateway.read_bitable = lambda *args: [["字段"], ["嵌入多维表格内容"]]
    adapter = BlocksAdapter(gateway)
    blocks, _ = adapter.adapt([{"block_id": "root", "block_type": 1, "children": ["table"]},
                              {"block_id": "table", "block_type": 18, "bitable": {"token": "app_tbl_view"}}], "root")
    assert "嵌入多维表格内容" in blocks[0]["markdown"]
    gateway.read_bitable = lambda *args: [["字段"], ["已修改"]]
    with pytest.raises(SnapshotError, match="embedded_content_changed"):
        adapter.verify_embedded()


def test_one_failed_wiki_does_not_prevent_successful_scan(online):
    service, api, _ = online
    other = row("node00000002", "obj00000002", method="人工")
    other[3] = "不可访问库"
    api.rows.append(other)
    api.nodes[other[12]] = dict(api.nodes[api.members[0]], node_token=other[12], obj_token=other[13])
    api.config.append(["不可访问库", True, "https://example.feishu.cn/wiki/parent00000002", ""])
    service.sync(write=True)
    assert jobs(service) and api.rows[2][7] != "已移出待审核"


def test_corrupt_snapshot_blocks_model(online):
    service, api, model = online
    service.sync(write=True)
    (Path(jobs(service)[0]["snapshot"]) / "source.pdf").write_bytes(b"broken")
    service.review(write=True)
    assert model.call_count == 0 and jobs(service)[0]["status"] == "obsolete"


def test_gateway_reads_all_sheet_rows_including_blank_gap():
    ranges = []
    def handler(req):
        if "auth" in req.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "fake"})
        if req.url.path.endswith("query"):
            data = {"sheets": [{"sheet_id": "sheet", "grid_properties": {"row_count": 2200, "column_count": 14}}]}
        else:
            ranges.append(req.url.path)
            values = [["last-row"]] if "A2001" in req.url.path else [["first-row"]] if "A1:" in req.url.path else []
            data = {"valueRange": {"values": values}}
        return httpx.Response(200, json={"code": 0, "data": data})
    gateway = FeishuGateway(FeishuSettings(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    values = gateway.read_embedded_sheet("app", "sheet")
    assert len(values) == 2001 and values[2000] == ["last-row"] and len(ranges) == 3


def test_migration_pauses_unconfirmed_delivery_and_reports_missing(online, tmp_path):
    service, api, model = online
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "ai_review_delivery_failures.json").write_text(json.dumps({api.members[0]: {"next_round": 1}}), encoding="utf-8")
    report = migrate(service, legacy, write=True)
    assert report["paused"] and service.state.control(api.members[0])["paused"]
    service.sync(write=True)
    assert not jobs(service) and model.call_count == 0


def test_pass_waits_for_publication_and_admin_not_duplicated(online):
    service, api, model = online
    model.fixture_response["review"].update(result="pass", pass_reason="已有实践过程", issues=[], major_count=0,
                                           suggested_next_action="admin_confirm", revision_priority=[])
    service.sync(write=True)
    service.review(write=True)
    service.retry(write=True)
    assert api.rows[1][7:9] == ["AI通过待确认", 1], service.events
    assert not service.index_store.query(exclude_document_id="other")
    service.sync(write=True)
    service.retry(write=True)
    assert len(api.messages) == 2
    api.rows[1][11] = VERSION
    service.index(write=True)
    assert len(service.index_store.query(exclude_document_id="other")) == 1


def test_published_human_article_is_retrieved_by_v2(online):
    service, api, model = online
    api.rows[1][11] = VERSION
    api.rows[1][5] = "人工"
    service.index(write=True)
    new = row("node00000002", "obj00000002")
    api.rows.append(new)
    api.nodes[new[12]] = dict(api.nodes[api.members[0]], node_token=new[12], obj_token=new[13])
    api.members.append(new[12])
    service.sync(write=True)
    service.review(write=True)
    audit = json.loads((Path(jobs(service)[0]["run_dir"]) / "similarity_retrieval.json").read_text(encoding="utf-8"))
    assert audit["index_candidate_count"] == 1 and audit["prompt_candidates"]
    assert audit["prompt_candidates"][0]["document_id"] == api.rows[1][13]


def test_failed_directory_in_same_wiki_does_not_mark_missing(online):
    service, api, _ = online
    api.rows[1][5] = "人工"
    api.members = []
    api.config.append(["测试库", True, "https://example.feishu.cn/wiki/parent00000002", ""])
    service.sync(write=True)
    assert api.rows[1][7] != "已移出待审核"


def test_outdated_assignment_notification_cancelled(online):
    service, api, _ = online
    api.rows[1][5] = ""
    service.sync(write=True)
    assert service.state.items("delivery")
    api.rows[1][5] = "人工"
    api.rows[1][7] = "人工审稿中"
    service.retry(write=True)
    assert not api.messages
    assert all(v["status"] == "cancelled" for _, v in service.state.items("delivery"))


def test_configuration_environment_priority_and_separate_roots(tmp_path, monkeypatch):
    from wiki_review_v2.online.config import load_settings
    config = tmp_path / "online.json"
    config.write_text(json.dumps({"online_root": "production", "feishu": {"app_id": "file-id", "app_secret": "file-secret"},
                                  "model": {"kimi_model": "file-model", "similarity_threshold": 0.2}}), encoding="utf-8")
    monkeypatch.setenv("FEISHU_APP_ID", "env-id")
    monkeypatch.setenv("KIMI_MODEL", "env-model")
    monkeypatch.delenv("SIMILARITY_THRESHOLD", raising=False)
    monkeypatch.delenv("WIKI_V2_ONLINE_ROOT", raising=False)
    monkeypatch.setenv("SIMILARITY_MIN_SCORE", "0.7")
    feishu, settings, root = load_settings(config)
    assert feishu.app_id == "env-id" and settings.kimi_model == "env-model" and settings.similarity_threshold == 0.7
    assert root == tmp_path / "production" and settings.output_root.parent == root
    assert "file-secret" not in repr(feishu) and not root.exists()


def test_export_pdf_polls_real_response_shape():
    requests, polls = [], []
    pdf = MockFeishu().export_pdf("obj", 100000)
    def handler(req):
        requests.append(req)
        if "auth" in req.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "fake"})
        if req.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"ticket": "ticket"}})
        if req.url.path.endswith("download"):
            return httpx.Response(200, content=pdf)
        polls.append(1)
        result = {"job_status": 1} if len(polls) == 1 else {"job_status": 0, "file_token": "exported"}
        return httpx.Response(200, json={"code": 0, "data": {"result": result}})
    gateway = FeishuGateway(FeishuSettings(), client=httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None)
    assert gateway.export_pdf("obj", 100000) == pdf
    assert len(polls) == 2
    assert json.loads(next(r.content for r in requests if r.url.path.endswith("export_tasks"))) == {
        "type": "docx", "token": "obj", "file_extension": "pdf"}
