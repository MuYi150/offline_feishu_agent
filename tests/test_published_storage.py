import sqlite3
from dataclasses import fields

import pytest

from wiki_review_v2.online.published import PublishedIndex
from wiki_review_v2.online.state import StateStore
from wiki_review_v2.similarity_index import IndexedSimilarityArticle


def publication():
    article = {f.name: [] if f.name in PublishedIndex.LIST_FIELDS else "" 
               for f in fields(IndexedSimilarityArticle)}
    article.update(document_id="doc", title="公示文章", content="可直接查看的完整检索概述",
                   source_updated_at="v1", topics=["机器人"])
    return {"article": article, "eligible": True, "publication_confirmed": True}


def test_legacy_migration_preserves_content_and_journal(tmp_path):
    state = StateStore(tmp_path)
    value = publication()
    state.put("publication", "doc", value)
    state.put("job", "job1", {"status": "committed"})
    index = PublishedIndex(state)
    before = state.path.read_bytes()
    assert index.query(exclude_document_id="other")[0].content == value["article"]["content"]
    assert state.path.read_bytes() == before  # Reading must not migrate a dry-run.
    assert index.initialize() == 1
    assert index.initialize() == 0
    with sqlite3.connect(state.path) as connection:
        assert connection.execute("SELECT title,content,eligible,review_result FROM similarity_articles").fetchone() == (
            "公示文章", value["article"]["content"], 1, "")
    assert state.get("publication_archive", "doc") == value
    assert state.items("publication") == []
    assert state.get("job", "job1") == {"status": "committed"}
    index.invalidate({"doc": "v2"})
    assert index.query(exclude_document_id="other") == []
    index.invalidate({"doc": "v1"})
    assert len(index.query(exclude_document_id="other")) == 1
    assert index.query(exclude_document_id="doc") == []


def test_new_storage_updates_and_requires_publication(tmp_path):
    index = PublishedIndex(StateStore(tmp_path))
    assert index.entries() == []
    assert not index.path.exists()
    value = publication()
    index.put(value)
    value["article"]["content"] = "更新后的概述"
    index.put(value)
    assert len(index.entries()) == 1
    assert index.query(exclude_document_id="other")[0].content == "更新后的概述"
    value["publication_confirmed"] = False
    index.put(value)
    assert index.query(exclude_document_id="other") == []


def test_invalid_legacy_identity_does_not_remove_original(tmp_path):
    state = StateStore(tmp_path)
    state.put("publication", "wrong-id", publication())
    with pytest.raises(ValueError, match="身份不一致"):
        PublishedIndex(state).initialize()
    assert state.get("publication", "wrong-id") == publication()
    assert state.items("publication_archive") == []
