from __future__ import annotations

from datetime import UTC, datetime
from contextlib import closing
from dataclasses import asdict, fields
import json
import sqlite3

from ..similarity_index import IndexedSimilarityArticle


class PublishedIndex:
    """Publication eligibility is independent of the AI review outcome."""
    def __init__(self, store):
        self.store = store
        self.path = store.path

    LIST_FIELDS = {"topics", "technical_entities", "key_parameters", "methods",
                   "application_scenarios", "validation_methods"}

    @classmethod
    def columns(cls):
        return [(f.name, f.name + "_json" if f.name in cls.LIST_FIELDS else f.name)
                for f in fields(IndexedSimilarityArticle)]

    def entries(self):
        """Read either schema without creating or migrating files during dry-run."""
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            values = {k: v for k, v in self.store.items("publication")}
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='similarity_articles'").fetchone():
                for row in connection.execute("SELECT * FROM similarity_articles ORDER BY document_id"):
                    article = {name: json.loads(row[col]) if name in self.LIST_FIELDS else row[col]
                               for name, col in self.columns()}
                    values[row["document_id"]] = {"article": article, "eligible": bool(row["eligible"]),
                                                   "publication_confirmed": bool(row["publication_confirmed"])}
        return list(values.items())

    def get(self, document_id):
        return next((value for key, value in self.entries() if key == document_id), None)

    def _write(self, connection, value, *, overwrite=True):
        article = asdict(IndexedSimilarityArticle(**value["article"]))
        columns = [col for _, col in self.columns()] + ["keywords_json", "eligible", "publication_confirmed"]
        values = [json.dumps(article[name], ensure_ascii=False) if name in self.LIST_FIELDS else article[name]
                  for name, _ in self.columns()]
        values += [json.dumps(article["topics"], ensure_ascii=False), int(bool(value.get("eligible"))),
                   int(bool(value.get("publication_confirmed")))]
        conflict = ("DO UPDATE SET " + ",".join(f"{col}=excluded.{col}" for col in columns if col != "document_id")) if overwrite else "DO NOTHING"
        connection.execute(f"INSERT INTO similarity_articles ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                           f"ON CONFLICT(document_id) {conflict}", values)

    def initialize(self):
        """Atomically migrate JSON publications; retain originals as audit records."""
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Validate every legacy article before changing the schema or archiving it.
            old = connection.execute("SELECT key,payload FROM records WHERE kind='publication'").fetchall()
            legacy = [(key, json.loads(raw)) for key, raw in old]
            for key, value in legacy:
                article = IndexedSimilarityArticle(**value["article"])
                if article.document_id != key:
                    raise ValueError("公示索引身份不一致，停止迁移")
            definitions = [f"{col} TEXT {'PRIMARY KEY' if name == 'document_id' else 'NOT NULL'}"
                           for name, col in self.columns()]
            connection.execute("CREATE TABLE IF NOT EXISTS similarity_articles (" + ",".join(definitions) +
                               ",keywords_json TEXT NOT NULL,eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),"
                               "publication_confirmed INTEGER NOT NULL CHECK(publication_confirmed IN (0,1)))")
            for key, value in legacy:
                self._write(connection, value, overwrite=False)
                connection.execute("INSERT OR IGNORE INTO records(kind,key,payload) SELECT 'publication_archive',key,payload "
                                   "FROM records WHERE kind='publication' AND key=?", (key,))
                connection.execute("DELETE FROM records WHERE kind='publication' AND key=?", (key,))
        return len(legacy)

    def put(self, value):
        self.initialize()
        with self.store.connection() as connection:
            self._write(connection, value)

    def query(self, *, exclude_document_id):
        return [IndexedSimilarityArticle(**v["article"]) for _, v in self.entries()
                if v.get("eligible") and v.get("publication_confirmed") and v["article"]["document_id"] != exclude_document_id]

    def refresh_metadata(self, record):
        value = self.get(record["obj"])
        if value and value["article"]["source_updated_at"] == record["updated"]:
            article = value["article"]
            updates = {"title": record["title"], "wiki_name": record["wiki_name"], "node_token": record["node"], "link": record["link"]}
            if any(article.get(k) != v for k, v in updates.items()):
                article.update(updates)
                self.put(value)

    def invalidate(self, valid_versions):
        self.initialize()
        for key, value in self.entries():
            article = value["article"]
            valid = value.get("publication_confirmed", False) and valid_versions.get(key) == article["source_updated_at"]
            if value["eligible"] != valid:
                value["eligible"] = valid
                self.put(value)

    def upsert_published(self, source, profile, overview, model):
        if not profile.source_content_hash or not overview.content.strip():
            raise ValueError("缺少可信公示文章概述")
        article = IndexedSimilarityArticle(
            document_id=source.document_id, title=source.title, wiki_name=source.wiki_name,
            node_token=source.node_token, link=source.link, content=overview.content, topics=overview.topics,
            technical_entities=overview.technical_entities, key_parameters=overview.key_parameters,
            methods=overview.methods, application_scenarios=overview.application_scenarios,
            validation_methods=overview.validation_methods, summary_source="ai_retrieval_overview",
            overview_model=model, overview_prompt_version="retrieval_overview_v1",
            source_content_hash=profile.source_content_hash, source=profile.source,
            review_result="", local_status="已公示", source_updated_at=source.updated_at,
            indexed_at=datetime.now(UTC).isoformat())
        self.put({"eligible": True, "publication_confirmed": True, "article": asdict(article)})
