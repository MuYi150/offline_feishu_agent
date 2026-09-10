from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..fixtures import DocumentExtractor
from ..model import KimiMultimodalModel, ModelRequest
from ..models import (FixtureOptions, ReviewHistoryRecord, ReviewResult, RetrievalArticleOverview,
                      RetrievalArticleOverviewAudit, SimilarityProfile, SourceDocument)
from ..retrieval_overview import RETRIEVAL_OVERVIEW_SYSTEM_PROMPT, RetrievalOverviewPromptBuilder, RetrievalOverviewValidator
from ..runner import ReviewRunner
from ..similarity_profile import SimilarityProfileBuilder
from .feishu import FeishuError
from .notifications import render_messages, split_message
from .published import PublishedIndex
from .records import (AI_METHODS, AI_STATES, COLUMNS, digest, eligible, expected_status, link, newer,
                      published, rows_to_records, source_document, text, timestamp, token)
from .snapshots import BlocksAdapter, SnapshotBuilder, SnapshotError
from .state import OnlineHistoryStore, StateStore


class OnlineService:
    """Public mutating operations must run under StateStore.lock (CLI does so once)."""
    def __init__(self, gateway, feishu_settings, model_settings, root, *, model_override=None):
        self.api, self.config, self.settings, self.root = gateway, feishu_settings, model_settings, Path(root)
        self.state = StateStore(self.root)
        self.index_store = PublishedIndex(self.state)
        self.snapshots = SnapshotBuilder(gateway, model_settings, self.root)
        self.model_override = model_override
        self.events = []

    def event(self, action, **values):
        self.events.append({"action": action, **values})

    def error(self, action, node, exc, *, write):
        code = getattr(exc, "code", str(exc) if isinstance(exc, (SnapshotError, ValueError)) else type(exc).__name__)
        self.event(action, node=node, error=code)
        if write:
            self.state.put("error", digest([action, node]), {"action": action, "node": node, "error": code})
            self.admin_event(f"error:{action}:{node}:{code}", f"线上审稿流程需要处理\n文档节点：{node}\n阶段：{action}\n原因：{code}")

    def records(self):
        return rows_to_records(self.api.read_sheet("review"))

    @staticmethod
    def match(document, records):
        for key, value in (("node", document["node"]), ("obj", document["obj"]), ("link", document["link"])):
            if not value:
                continue
            matches = [r for r in records if r[key] == value]
            if len(matches) > 1:
                raise ValueError("同一文档匹配到多行，请先修复重复记录")
            if matches:
                return matches[0]
        fallback = lambda r: ("".join(r["title"].lower().split()), r["author"].lstrip("@"), r["wiki_name"])
        matches = [r for r in records if fallback(r) == fallback(document)]
        if len(matches) > 1:
            raise ValueError("文档兜底标识不唯一")
        return matches[0] if matches else None

    def node_document(self, node, wiki_name, existing=None):
        owner = {}
        try:
            owner = self.api.user(node.get("owner"))
        except Exception:
            pass
        domain = self.config.domain.removeprefix("https://").rstrip("/")
        return {"node": node["node_token"], "obj": node["obj_token"], "title": node.get("title", ""),
                "link": f"https://{domain}/wiki/{node['node_token']}", "wiki_name": wiki_name,
                "author": "@" + owner["name"] if owner.get("name") else (existing or {}).get("author", ""),
                "updated": timestamp(node.get("edit_time"))}

    def update_record(self, record, updates, *, write):
        if not updates:
            return record
        self.event("update", node=record["node"], row=record["row_index"], columns=list(updates))
        if write:
            self.api.update_cells("review", record["row_index"], updates)
        row = list(record["row"])
        for col, value in updates.items():
            row[ord(col) - 65] = value
        updated = rows_to_records([COLUMNS, row])[0]
        updated["row_index"] = record["row_index"]
        record.update(updated)
        return record

    def refresh(self, record, node, *, write):
        doc = self.node_document(node, record["wiki_name"], record)
        if write and record["node"] != doc["node"] and record["obj"] == doc["obj"]:
            old_node = record["node"]
            self.state.put("control", doc["node"], self.state.control(old_node))
            for key, value in self.state.items("history"):
                if key.startswith(old_node + ":"):
                    self.state.add("history", doc["node"] + key[len(old_node):], value)
            self.state.put("identity_alias", old_node, {"node": doc["node"], "obj": doc["obj"]})
        mapping = {"title": "B", "link": "C", "author": "E", "updated": "J", "node": "M", "obj": "N"}
        updates = {col: doc[key] for key, col in mapping.items() if doc[key] != record[key]}
        # record['node'] can be inferred from C even when M is empty.
        if text(record["row"][12]) != doc["node"]:
            updates["M"] = doc["node"]
        expected_link = {"text": doc["title"], "link": doc["link"], "type": "url"}
        if record["row"][2] != expected_link:
            updates["C"] = expected_link
        return self.update_record(record, updates, write=write)

    def sync(self, *, write=False, extract_only=False, node=None):
        selected_node = node
        rows = self.api.read_sheet("review")
        records = rows_to_records(rows)
        configs = self.api.read_sheet("config")
        if not configs or [text(x) for x in configs[0][:4]] != ["知识库名称", "是否启用同步", "待审核目录链接", "待审核目录 node_token"]:
            raise ValueError("知识库配置表 A:D 表头不符合约定")
        seen, scanned, refreshed, failed_wikis = set(), set(), set(), set()
        # Validate identity uniqueness before any write.
        for r in records:
            self.match(r, records)
        for r in records:
            try:
                node = self.api.node(r["node"])
                self.refresh(r, node, write=write)
                refreshed.add(r["node"])
            except Exception as exc:
                self.error("metadata", r["node"], exc, write=write)
        next_row = len(rows) + 1
        for config_row, raw in enumerate(configs[1:], 2):
            cells = list(raw) + [""] * max(0, 4 - len(raw))
            if text(cells[1]).lower() not in {"true", "1", "yes", "是", "启用"} or not cells[2]:
                continue
            wiki = text(cells[0])
            try:
                parent_token = token(cells[2])
                if not parent_token:
                    raise ValueError("待审核目录链接无法解析")
                parent = self.api.node(parent_token)
                children = self.api.children(parent)
                seen.update(n["node_token"] for n in children)
                scanned.add(wiki)
                if not text(cells[3]):
                    self.event("config_token", row=config_row)
                    if write:
                        self.api.update_cells("config", config_row, {"D": parent_token})
                for node in children:
                    if not node.get("obj_token"):
                        continue
                    doc = self.node_document(node, wiki)
                    existing = self.match(doc, records)
                    if existing:
                        self.refresh(existing, node, write=write)
                        if existing["wiki_name"] != wiki:
                            self.update_record(existing, {"D": wiki}, write=write)
                        refreshed.add(existing["node"])
                    else:
                        row = [len(records) + 1, doc["title"], {"text": doc["title"], "link": doc["link"], "type": "url"},
                               wiki, doc["author"], "", "", "已投稿", 0, doc["updated"], "", "", doc["node"], doc["obj"]]
                        self.event("insert", node=doc["node"], row=next_row)
                        if write:
                            self.api.repair_format(next_row, next_row - 1 if next_row > 2 else None)
                            self.api.insert_row(next_row, row)
                        record = rows_to_records([COLUMNS, row])[0]
                        record["row_index"] = next_row
                        records.append(record)
                        refreshed.add(record["node"])
                        next_row += 1
            except Exception as exc:
                failed_wikis.add(wiki)
                self.error("scan", wiki, exc, write=write)
        scanned.difference_update(failed_wikis)
        for r in records:
            control = self.state.control(r["node"])
            if r["wiki_name"] in scanned and r["node"] not in seen and not published(r) and r["status"] != "已拒稿":
                self.update_record(r, {"H": "已移出待审核"}, write=write)
            elif r["node"] in refreshed:
                reset = r["status"] == "已拒稿" and r["method"] in AI_METHODS and newer(r["updated"], r["reviewed"]) and not published(r)
                if reset:
                    # Journal the cycle transition BEFORE its remote write, so a crash cannot reuse old history.
                    marker = digest([r["updated"], r["obj"], r["reviewed"]])
                    if control.get("reset_marker") != marker:
                        control = {"cycle": control["cycle"] + 1, "handoff": False, "reset_marker": marker}
                        if write:
                            self.state.put("control", r["node"], control)
                    self.update_record(r, {"H": "AI审稿中", "I": 0}, write=write)
                else:
                    status = expected_status(r, handoff=control["handoff"], pending=self.state.pending(r["node"]))
                    if status != r["status"]:
                        self.update_record(r, {"H": status}, write=write)
            if write and r["status"] == "已投稿" and not r["method"]:
                self.admin_event(f"assign:{r['node']}:{r['updated']}", f"请为新投稿选择审稿方式\n{r['title']}\n{r['link']}")
            if write and r["status"] == "AI通过待确认":
                self.admin_event(f"pass:{r['node']}:{r['round']}:{r['reviewed']}", f"AI通过待管理员确认\n{r['title']}\n{r['link']}")
            if eligible(r) and r["node"] in refreshed and not control["handoff"] and (not selected_node or r["node"] == selected_node):
                self.event("fixture_candidate", node=r["node"], round=r["round"])
                if write or extract_only:
                    self.capture(r, write=write)
        order = [r["node"] for r in sorted(records, key=lambda x: x["updated"] or "9999")]
        self.event("sort_preview", needed=order != [r["node"] for r in records], node_order=order)
        if write:
            self.reconcile_publications(records)
        return records

    def capture(self, record, *, write):
        try:
            control = self.state.control(record["node"])
            if control.get("paused"):
                raise ValueError("迁移或历史状态待管理员处理：" + control["paused"])
            history = OnlineHistoryStore(self.state, record["node"], control["cycle"])
            latest = history.load_latest(record["obj"])
            if latest and latest.review_round != record["round"]:
                raise ValueError("表格轮次与已提交历史不一致，暂停审稿")
            if record["round"] and not history.load_latest_for_round(record["obj"], record["round"]):
                raise ValueError("缺少对应轮次的真实历史，暂停复审")
            key, path, manifest = self.snapshots.capture(record, control["cycle"])
            if write:
                self.state.add("job", key, {"id": key, "node": record["node"], "cycle": control["cycle"],
                                           "record": record, "snapshot": str(path), "status": "queued"})
            self.event("fixture_ready", node=record["node"], path=str(path))
            return key
        except Exception as exc:
            self.error("capture", record["node"], exc, write=write)
            return None

    def current_for_job(self, job, *, committed=False):
        record = job["record"]
        candidates = [r for r in self.records() if r["node"] == record["node"]]
        if len(candidates) != 1:
            raise SnapshotError("row_missing_or_duplicate")
        current = candidates[0]
        SnapshotBuilder.verify_node(record, self.api.node(record["node"]))
        # A document may move out before the next sync; never trust its cached sheet status.
        configs = self.api.read_sheet("config")
        parents = [token((list(row) + [""] * 4)[2]) for row in configs[1:]
                   if text((list(row) + [""] * 4)[0]) == current["wiki_name"]
                   and text((list(row) + [""] * 4)[1]).lower() in {"true", "1", "yes", "是", "启用"}]
        if not parents or not any(record["node"] in {n["node_token"] for n in self.api.children(self.api.node(p))} for p in parents):
            raise SnapshotError("document_not_in_enabled_pending_directory")
        if any(current[k] != record[k] for k in ("obj", "updated", "method", "wiki_name", "author")) or published(current):
            raise SnapshotError("sheet_version_or_method_changed")
        if committed:
            if current["round"] != record["round"] + 1 or current["status"] != job["target_status"] or current["reviewed"] != job["reviewed_at"]:
                raise SnapshotError("sheet_result_changed")
        elif current["round"] != record["round"] or current["status"] != record["status"] or not eligible(current):
            raise SnapshotError("sheet_review_state_changed")
        if self.state.control(record["node"])["cycle"] != job["cycle"]:
            raise SnapshotError("review_cycle_changed")
        return current

    def runner(self, job):
        return ReviewRunner(self.settings, history_store=OnlineHistoryStore(self.state, job["node"], job["cycle"]),
                            similarity_store=self.index_store, defer_commit=True)

    def review(self, *, write=False, real_model=False, limit=1, node=None):
        if limit < 1:
            raise ValueError("limit 必须大于 0")
        if not write:
            if real_model:
                return self.preview(limit=limit, node=node)
            for key, job in self.state.items("job"):
                if job["status"] in {"queued", "reviewing", "review_failed"} and (not node or node == job["node"]):
                    self.event("review_plan", job=key, node=job["node"])
            return
        if not real_model and self.model_override is None:
            raise ValueError("线上审稿必须显式 --real-model")
        self.reconcile_publications(self.records())
        processed = 0
        for key, job in self.state.items("job"):
            if job["status"] not in {"queued", "reviewing", "review_failed"} or (node and node != job["node"]):
                continue
            if processed >= limit:
                break
            try:
                current = self.current_for_job(job)
                if self.state.control(job["node"])["handoff"]:
                    raise SnapshotError("manual_handoff")
                if self.state.control(job["node"]).get("paused"):
                    raise ValueError("迁移状态待管理员处理")
                if current["round"] >= self.config.max_review_rounds:
                    control = self.state.control(job["node"])
                    self.state.put("control", job["node"], dict(control, handoff=True))
                    self.update_record(current, {"H": "待分配人工审稿"}, write=True)
                    raise SnapshotError("round_limit_manual_handoff")
                SnapshotBuilder.verify_files(Path(job["snapshot"]))
                processed += 1
                runner = self.runner(job)
                output = Path(job.get("run_dir") or self.settings.output_root / key / "review")
                job.update(status="reviewing", run_dir=str(output))
                self.state.put("job", key, job)
                if (output / "run_metadata.json").exists():
                    summary = runner.resume(output, explicit_real_authorization=True, model_override=self.model_override)
                else:
                    summary = runner.run_snapshot(Path(job["snapshot"]), run_id="review", real_model=True,
                                                  explicit_real_authorization=True, model_override=self.model_override)
                if not summary.ok:
                    job["status"] = "review_failed"
                    self.state.put("job", key, job)
                    self.error("review", job["node"], ValueError(str((summary.failure or {}).get("code", "review_failed"))), write=True)
                    continue
                result = ReviewResult.model_validate_json((output / "parsed_review_result.json").read_text(encoding="utf-8"))
                target = result.local_status
                handoff = result.result.value in {"recommend_human_review", "incomplete_review"} or (
                    result.result.value == "need_revision" and current["round"] + 1 >= self.config.max_review_rounds)
                if handoff:
                    target = "待分配人工审稿"
                job.update(status="reviewed", target_status=target, handoff=handoff,
                           reviewed_at=timestamp(datetime.now().isoformat()), result=result.model_dump(mode="json"))
                self.state.put("job", key, job)
                self.deliver(job)
            except SnapshotError as exc:
                job["status"] = "obsolete"
                job["obsolete_reason"] = str(exc)
                self.state.put("job", key, job)
                self.error("obsolete", job["node"], exc, write=True)
            except Exception as exc:
                self.error("review_or_delivery", job["node"], exc, write=True)

    def deliver(self, job):
        key = job["id"]
        if job["status"] in {"reviewed", "sheet_pending"}:
            # Store intended values before dispatch. Retry recognizes an already-applied write.
            try:
                current = self.current_for_job(job, committed=True)
                applied = True
            except SnapshotError:
                current = self.current_for_job(job)
                applied = False
            if not applied:
                job["status"] = "sheet_pending"
                self.state.put("job", key, job)
                self.api.update_cells("review", current["row_index"], {"H": job["target_status"],
                                      "I": job["record"]["round"] + 1, "K": job["reviewed_at"]})
                self.current_for_job(job, committed=True)
            job["status"] = "sheet_written"
            self.state.put("job", key, job)
        if job["status"] == "sheet_written":
            self.current_for_job(job, committed=True)
            result = ReviewResult.model_validate(job["result"])
            history = ReviewHistoryRecord(run_id=key, review_round=job["record"]["round"] + 1,
                                          completed_at=job["reviewed_at"], result=result)
            OnlineHistoryStore(self.state, job["node"], job["cycle"]).append(job["record"]["obj"], history)
            if job["handoff"]:
                control = self.state.control(job["node"])
                self.state.put("control", job["node"], dict(control, handoff=True))
            source = SourceDocument.model_validate_json((Path(job["snapshot"]) / "source_document.json").read_text(encoding="utf-8"))
            author, admin = render_messages(source, result, job["target_status"], handoff=job["handoff"] and result.result.value == "need_revision")
            self.enqueue(key, "author", source.author_id, author, job_id=key)
            if self.config.enable_admin_notify:
                self.enqueue(key, "admin", self.config.admin_open_id, admin, job_id=key)
            job["status"] = "committed"
            self.state.put("job", key, job)
            if result.result.value == "pass":
                self.state.put("notified", f"pass:{job['node']}:{source.review_round + 1}:{job['reviewed_at']}", True)
        self.event("result_committed", job=key, status=job["target_status"])

    def enqueue(self, event_key, role, recipient, content, *, job_id=None):
        for part, message in enumerate(split_message(content)):
            key = digest([event_key, role, part])[:40]
            self.state.add("delivery", key, {"id": key, "event": event_key, "role": role, "recipient": recipient,
                                             "content": message, "part": part, "job_id": job_id, "status": "pending"})

    def admin_event(self, key, content):
        if self.config.enable_admin_notify and not self.state.get("notified", key):
            self.enqueue(key, "admin", self.config.admin_open_id, content)

    def retry(self, *, write=False, node=None, event=None):
        for key, job in self.state.items("job"):
            if job["status"] not in {"reviewed", "sheet_pending", "sheet_written"} or (node and node != job["node"]):
                continue
            if event:
                continue
            self.event("delivery_plan", job=key)
            if write:
                try:
                    self.deliver(job)
                except SnapshotError as exc:
                    job.update(status="obsolete", obsolete_reason=str(exc))
                    self.state.put("job", key, job)
                    self.error("obsolete", job["node"], exc, write=True)
                except Exception as exc:
                    self.error("sheet_delivery", job["node"], exc, write=True)
        blocked_events = set()
        for key, item in self.state.items("delivery"):
            if event and item["event"] != event:
                continue
            group = (item["event"], item["role"])
            job = self.state.get("job", item.get("job_id")) if item.get("job_id") else None
            if node and (not job or job["node"] != node):
                continue
            if item["status"] == "sending" and write:
                item["status"] = "uncertain"
                self.state.put("delivery", key, item)
            if item["status"] in {"uncertain", "sending"}:
                blocked_events.add(group)
            if item["status"] not in {"pending", "failed"} or group in blocked_events:
                continue
            self.event("message_plan", delivery=key, role=item["role"])
            if not write:
                continue
            if not job and item["event"].startswith(("assign:", "pass:")):
                event_node = item["event"].split(":", 2)[1]
                try:
                    current = next((r for r in self.records() if r["node"] == event_node), None)
                    active = current and not published(current) and (
                        item["event"] == f"assign:{event_node}:{current['updated']}" and current["status"] == "已投稿" and not current["method"] or
                        item["event"] == f"pass:{event_node}:{current['round']}:{current['reviewed']}" and current["status"] == "AI通过待确认")
                    if not active:
                        item["status"] = "cancelled"
                        self.state.put("delivery", key, item)
                        continue
                except Exception as exc:
                    self.error("admin_notification_check", event_node, exc, write=True)
                    blocked_events.add(group)
                    continue
            if job:
                try:
                    if job["status"] != "committed":
                        continue
                    self.current_for_job(job, committed=True)
                except SnapshotError:
                    item["status"] = "cancelled"
                    self.state.put("delivery", key, item)
                    continue
                except Exception as exc:
                    self.error("notification_check", job["node"], exc, write=True)
                    blocked_events.add(group)
                    continue
            if not item["recipient"]:
                try:
                    item["recipient"] = self.config.admin_open_id if item["role"] == "admin" else self.api.user(self.api.node(job["node"]).get("owner")).get("open_id", "")
                except Exception:
                    pass
            item["status"] = "sending"
            self.state.put("delivery", key, item)
            try:
                receipt = self.api.send_message(item["recipient"], item["content"], key)
                item.update(status="sent", message_id=receipt)
            except FeishuError as exc:
                item.update(status="uncertain" if exc.uncertain else "failed", error=exc.code)
                blocked_events.add(group)
            except Exception:
                item.update(status="uncertain", error="unknown_send_outcome")
                blocked_events.add(group)
            self.state.put("delivery", key, item)
            if item["status"] != "sent" and item["role"] == "author":
                self.admin_event(f"delivery_failure:{key}", f"投稿人通知待处理\n文档：{job['record']['title']}\n投递编号：{key}\n状态：{item['status']}\n不会重新调用模型。")
        for _, item in self.state.items("delivery"):
            if item["status"] == "sent" and all(d["status"] == "sent" for _, d in self.state.items("delivery") if d["event"] == item["event"]):
                self.state.put("notified", item["event"], True) if write else None

    def reconcile_publications(self, records):
        valid = {}
        for r in records:
            if published(r):
                try:
                    SnapshotBuilder.verify_node(r, self.api.node(r["node"]))
                    valid[r["obj"]] = r["updated"]
                    self.index_store.refresh_metadata(r)
                except Exception:
                    pass
        self.index_store.invalidate(valid)
        return valid

    def index(self, *, write=False, real_model=False, limit=None):
        records = self.records()
        valid = self.reconcile_publications(records) if write else {r["obj"]: r["updated"] for r in records if published(r)}
        processed = 0
        for record in records:
            if record["obj"] not in valid:
                continue
            old = self.index_store.get(record["obj"])
            if old and old["eligible"] and old["article"]["source_updated_at"] == record["updated"]:
                continue
            if limit is not None and processed >= limit:
                break
            self.event("publication_index_plan", node=record["node"])
            if not write:
                continue
            try:
                processed += 1
                SnapshotBuilder.verify_node(record, self.api.node(record["node"]))
                source = source_document(record, "published-" + digest(record["node"])[:16])
                profile, overview, model_name = self.cached_overview(record)
                if overview is None:
                    if not real_model and self.model_override is None:
                        self.event("index_waiting_for_model_authorization", node=record["node"])
                        continue
                    blocks, _ = BlocksAdapter(self.api).adapt(self.api.blocks(record["obj"]), record["obj"])
                    extracted = DocumentExtractor().extract(blocks)
                    # Published indexing never uses OCR or historical images. Raw text fallback is read-only.
                    if not extracted["content_markdown"].strip():
                        extracted["content_markdown"] = self.api.raw_content(record["obj"])
                    profile = SimilarityProfileBuilder(self.settings).build(source=source, extracted_content=extracted,
                                             case_path=self.root / "no-historical-files", options=FixtureOptions())
                    if not profile.query_text:
                        raise ValueError("公示文章没有可用文字，索引暂停")
                    model = self.model_override or KimiMultimodalModel(self.settings)
                    request = ModelRequest(phase="retrieval_overview", prompt=RetrievalOverviewPromptBuilder(self.settings).build(source=source, profile=profile),
                                           pages=[], schema_name="retrieval_article_overview", json_schema=RetrievalArticleOverview.model_json_schema(),
                                           system_prompt=RETRIEVAL_OVERVIEW_SYSTEM_PROMPT, model=self.settings.retrieval_overview_model or None)
                    response = model.invoke(request)
                    overview = RetrievalArticleOverview.model_validate_json(response.content)
                    model_name = self.settings.retrieval_overview_model or self.settings.kimi_model
                    overview, audit = RetrievalOverviewValidator(self.settings).normalize_and_validate(
                        document_id=source.document_id, overview=overview, profile=profile, model=model_name)
                    if not audit.validation.valid:
                        raise ValueError("公示文章概述校验失败")
                    self.state.put("overview", digest([record["node"], record["obj"], record["updated"]]),
                                   {"profile": profile.model_dump(mode="json"), "overview": overview.model_dump(mode="json"), "model": model_name})
                SnapshotBuilder.verify_node(record, self.api.node(record["node"]))
                current = next((r for r in self.records() if r["node"] == record["node"]), None)
                if not current or not published(current) or current["updated"] != record["updated"]:
                    raise SnapshotError("publication_changed_during_indexing")
                self.index_store.upsert_published(source, profile, overview, model_name)
                self.event("publication_indexed", node=record["node"])
            except Exception as exc:
                self.error("index", record["node"], exc, write=write)

    def cached_overview(self, record):
        cached = self.state.get("overview", digest([record["node"], record["obj"], record["updated"]]))
        if cached:
            return SimilarityProfile.model_validate(cached["profile"]), RetrievalArticleOverview.model_validate(cached["overview"]), cached["model"]
        for _, job in reversed(self.state.items("job")):
            if job["status"] != "committed" or any(job["record"][k] != record[k] for k in ("node", "obj", "updated")):
                continue
            output = Path(job["run_dir"])
            audit = RetrievalArticleOverviewAudit.model_validate_json((output / "retrieval_article_overview.json").read_text(encoding="utf-8"))
            profile = SimilarityProfile.model_validate_json((output / "similarity_profile.json").read_text(encoding="utf-8"))
            if audit.validation.valid and audit.overview and audit.source_content_hash == profile.source_content_hash:
                return profile, audit.overview, audit.model
        return None, None, ""

    def preview(self, *, limit, node=None):
        from ..storage import utc_run_id
        preview_root = self.root / "previews" / utc_run_id()
        settings = self.settings.model_copy(update={"output_root": preview_root / "outputs", "state_root": preview_root / "history"})
        service = OnlineService(self.api, self.config, settings, preview_root, model_override=self.model_override)
        # Copy only validated local state; every preview mutation stays below previews/<id>.
        for kind in ("history", "control"):
            for key, value in self.state.items(kind):
                service.state.put(kind, key, value)
        for _, value in self.index_store.entries():
            service.index_store.put(value)
        count = 0
        for record in self.records():
            if not eligible(record) or (node and record["node"] != node) or count >= limit:
                continue
            key = service.capture(record, write=True)
            if not key:
                self.events.extend(service.events)
                continue
            job = service.state.get("job", key)
            summary = service.runner(job).run_snapshot(Path(job["snapshot"]), real_model=True,
                       explicit_real_authorization=True, model_override=self.model_override)
            self.event("preview_result", node=record["node"], output=str(summary.output_dir), result=summary.result, failure=summary.failure)
            count += 1
