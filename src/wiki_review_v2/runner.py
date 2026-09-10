from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from .config import Settings
from .errors import FixtureError, ModelAuthenticationError, OutputConflictError, ReviewError, classify_exception
from .fixtures import FixtureDocumentSource
from .graph import ReviewWorkflow
from .model import FakeReviewModel, KimiMultimodalModel, ReviewModel
from .models import ReviewGraphState
from .similarity_index import SimilarityIndexStore
from .storage import AuditStore, atomic_write_json, utc_run_id


@dataclass(frozen=True)
class RunSummary:
    output_dir: Path
    run_id: str
    result: str | None
    failure: dict[str, Any] | None

    @property
    def ok(self) -> bool:
        return self.failure is None and self.result is not None


class ReviewRunner:
    def __init__(self, settings: Settings, *, history_store=None, similarity_store=None,
                 defer_commit: bool = False) -> None:
        self.settings = settings
        self.workflow_options = dict(history_store=history_store, similarity_store=similarity_store,
                                     defer_commit=defer_commit)
        self.source = FixtureDocumentSource()

    def run_snapshot(self, case_path: Path, **kwargs) -> RunSummary:
        """Run an immutable external fixture without changing the local fixture root."""
        path = case_path.resolve()
        runner = ReviewRunner(self.settings.model_copy(update={"fixtures_root": path.parent}),
                              **self.workflow_options)
        return runner.run_case(path.name, **kwargs)

    def initialize_similarity_index(self) -> dict[str, object]:
        store = SimilarityIndexStore(self.settings.similarity_index_path)
        store.initialize()
        return store.info()

    def similarity_index_info(self) -> dict[str, object]:
        return SimilarityIndexStore(self.settings.similarity_index_path).info()

    def list_cases(self) -> list[str]:
        return self.source.list_cases(self.settings.fixtures_root)

    def run_case(
        self,
        case_id: str,
        *,
        real_model: bool = False,
        explicit_real_authorization: bool = False,
        output_root: Path | None = None,
        run_id: str | None = None,
        model_override: ReviewModel | None = None,
    ) -> RunSummary:
        
        case_path = self.settings.fixtures_root / case_id
        bundle = self.source.load(case_path)                         #读取fixture包 
        if bundle.fixture_options.real_model_only and not real_model and model_override is None:
            raise FixtureError(f"Fixture {case_id} 标记为 real_model_only，请使用 --real-model")
        run_id = run_id or utc_run_id()
        root = (output_root or self.settings.output_root).resolve()
        output_dir = root / case_id / run_id                         #输出目录

        try:
            output_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise OutputConflictError(f"运行目录已存在，拒绝覆盖：{output_dir}") from exc

        mode = "real" if real_model else "fake"
        thread_id = f"{case_id}:{run_id}"
        metadata = {
            "case_id": case_id,
            "case_path": str(case_path.resolve()),
            "output_dir": str(output_dir),
            "run_id": run_id,
            "thread_id": thread_id,
            "model_mode": mode,
            "settings": self.settings.safe_dict(),
        }
        atomic_write_json(output_dir / "run_metadata.json", metadata)           #记录运行
        try:
            model = model_override or self._make_model(
                real_model, bundle.fake_model_response, explicit_real_authorization
            )
        except ReviewError as exc:
            failure = classify_exception(exc)
            AuditStore().save_failure(
                output_dir,
                failure,
                [{"node": "model_preflight", "status": "failed", "details": {"error": failure}}],
            )
            return RunSummary(output_dir=output_dir, run_id=run_id, result=None, failure=failure)
        
        initial = ReviewGraphState(                      #LangGraph 共用的状态对象，随着工作流状态增加
            case_id=case_id,
            case_path=str(case_path.resolve()),
            output_dir=str(output_dir),
            run_id=run_id,
            thread_id=thread_id,
            model_mode=mode,
        )
        return self._invoke(output_dir, initial.model_dump(mode="json"), model, resume=False)             #调用 LangGraph,返回运行结果

    def resume(self, run_dir: Path, *, explicit_real_authorization: bool = False,
               model_override: ReviewModel | None = None) -> RunSummary:
        output_dir = run_dir.resolve()
        metadata_path = output_dir / "run_metadata.json"
        if not metadata_path.exists():
            raise OutputConflictError("恢复目录缺少 run_metadata.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        case_path = Path(metadata["case_path"])
        bundle = self.source.load(case_path)
        real = metadata["model_mode"] == "real"
        try:
            model = model_override or self._make_model(real, bundle.fake_model_response, explicit_real_authorization)
        except ReviewError as exc:
            failure = classify_exception(exc)
            AuditStore().save_failure(
                output_dir,
                failure,
                [{"node": "model_preflight", "status": "failed", "details": {"error": failure}}],
            )
            return RunSummary(output_dir=output_dir, run_id=metadata["run_id"], result=None, failure=failure)
        return self._invoke(output_dir, None, model, resume=True)

    def _invoke(
        self,
        output_dir: Path,
        initial: dict[str, Any] | None,
        model: ReviewModel,
        *,
        resume: bool,
    ) -> RunSummary:
        metadata = json.loads((output_dir / "run_metadata.json").read_text(encoding="utf-8"))
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
        
        connection = sqlite3.connect(output_dir / "checkpoint.sqlite", check_same_thread=False)
        try:
            checkpointer = SqliteSaver(connection)
            graph = ReviewWorkflow(self.settings, model, **self.workflow_options).compile(checkpointer)
            config: dict[str, Any] = {"configurable": {"thread_id": metadata["thread_id"]}}
            if resume:
                latest = graph.get_state(config)
                if not latest.values:
                    initial = ReviewGraphState(**{k: metadata[k] for k in (
                        "case_id", "case_path", "output_dir", "run_id", "thread_id", "model_mode")})
                    final = graph.invoke(initial.model_dump(mode="json"), config=config)
                    data = final.model_dump(mode="json") if hasattr(final, "model_dump") else dict(final or {})
                    return RunSummary(output_dir, metadata["run_id"],
                                      (data.get("parsed_review_result") or {}).get("result"), data.get("failure"))
                failed_node = self._failed_node(latest.values)
                if failed_node:
                    for snapshot in graph.get_state_history(config):
                        if failed_node in snapshot.next:
                            config = snapshot.config
                            break
                final = graph.invoke(None, config=config)
            else:
                final = graph.invoke(initial, config=config)
        finally:
            connection.close()
        data = final.model_dump(mode="json") if hasattr(final, "model_dump") else dict(final or {})
        parsed = data.get("parsed_review_result") or {}
        return RunSummary(
            output_dir=output_dir,
            run_id=metadata["run_id"],
            result=parsed.get("result"),
            failure=data.get("failure"),
        )

    @staticmethod
    def _failed_node(values: Any) -> str | None:
        data = values.model_dump(mode="json") if hasattr(values, "model_dump") else dict(values or {})
        for event in reversed(data.get("trace") or []):
            if event.get("status") == "failed":
                return str(event.get("node"))
        return None

    def _make_model(
        self,
        real: bool,
        fake_response: dict[str, Any],
        explicit_real_authorization: bool,
    ) -> ReviewModel:
        if not real:
            return FakeReviewModel(fake_response)
        if not (explicit_real_authorization or self.settings.allow_real_model_call):
            raise ModelAuthenticationError("真实模型调用未显式启用")
        return KimiMultimodalModel(self.settings)
