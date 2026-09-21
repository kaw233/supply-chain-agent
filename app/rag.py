"""RAG contracts and persistence skeleton.

This module deliberately stops at interfaces and auditable metadata.  It does
not collect documents, call an embedding API, or fabricate vector search
results.  Real providers can be added behind the three adapter contracts.
"""
from __future__ import annotations

import hashlib
import re
from typing import Protocol

from .engine import DomainError
from .store import dump, load, now, uid


SECRET_KEY = re.compile(r"(token|secret|password|cookie|authorization|credential|api[_-]?key)", re.I)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EmbeddingProvider(Protocol):
    name: str

    def capabilities(self) -> dict: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    name: str

    def capabilities(self) -> dict: ...

    def upsert(self, collection_id: str, vectors: list[dict]) -> dict: ...

    def search(self, collection_id: str, vector: list[float], top_k: int, filters: dict) -> list[dict]: ...


class Reranker(Protocol):
    name: str

    def capabilities(self) -> dict: ...

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]: ...


class NullEmbeddingProvider:
    name = "none"

    def capabilities(self):
        return {"provider": self.name, "configured": False, "status": "not_configured"}

    def embed(self, texts):
        raise DomainError("Embedding Provider 尚未配置", 409)


class NullVectorStore:
    name = "none"

    def capabilities(self):
        return {"kind": self.name, "configured": False, "status": "not_configured"}

    def upsert(self, collection_id, vectors):
        raise DomainError("Vector Store 尚未配置", 409)

    def search(self, collection_id, vector, top_k, filters):
        raise DomainError("Vector Store 尚未配置", 409)


class NullReranker:
    name = "none"

    def capabilities(self):
        return {"kind": self.name, "configured": False, "status": "not_configured"}

    def rerank(self, query, candidates):
        raise DomainError("Reranker 尚未配置", 409)


def _clean_metadata(value, depth=0):
    if depth > 6:
        raise DomainError("RAG metadata 嵌套层级过深", 400)
    if isinstance(value, dict):
        result = {}
        for key, nested in value.items():
            if SECRET_KEY.search(str(key)):
                raise DomainError("RAG metadata 不得包含凭据字段", 400)
            result[str(key)[:100]] = _clean_metadata(nested, depth + 1)
        return result
    if isinstance(value, list):
        if len(value) > 100:
            raise DomainError("RAG metadata 数组最多 100 项", 400)
        return [_clean_metadata(item, depth + 1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else value[:4000]
    raise DomainError("RAG metadata 只能包含 JSON 基础类型", 400)


def _checksum(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _limit(value, default=5, maximum=50):
    try:
        return max(1, min(maximum, int(value or default)))
    except (TypeError, ValueError):
        raise DomainError("RAG top_k/limit 必须是整数", 400)


def preview_chunks(text, chunk_size=800, overlap=120):
    """Deterministic local chunk preview; it does not persist or collect data."""
    if not isinstance(text, str) or not text.strip():
        raise DomainError("chunk preview 需要非空文本", 400)
    try:
        size, overlap = int(chunk_size), int(overlap)
    except (TypeError, ValueError):
        raise DomainError("chunk_size 和 overlap 必须是整数", 400)
    if size < 100 or size > 12000 or overlap < 0 or overlap >= size:
        raise DomainError("chunk_size 需在 100–12000，overlap 需小于 chunk_size", 400)
    chunks = []
    start = 0
    ordinal = 0
    while start < len(text):
        end = min(len(text), start + size)
        part = text[start:end].strip()
        if part:
            chunks.append({
                "ordinal": ordinal,
                "text": part,
                "char_start": start,
                "char_end": end,
                "char_count": len(part),
                "token_count": None,
                "checksum": _checksum(part),
            })
            ordinal += 1
        if end >= len(text):
            break
        start = end - overlap
    return {"status": "preview", "persisted": False, "chunks": chunks, "count": len(chunks)}


class RAGService:
    def __init__(self, runtime, embedding=None, vector_store=None, reranker=None):
        self.rt = runtime
        self.store = runtime.store
        self.embedding = embedding or NullEmbeddingProvider()
        self.vector_store = vector_store or NullVectorStore()
        self.reranker = reranker or NullReranker()

    def capabilities(self):
        return {
            "enabled": True,
            "mode": "contract-only",
            "embedding": self.embedding.capabilities(),
            "vector_store": self.vector_store.capabilities(),
            "reranker": self.reranker.capabilities(),
            "features": {
                "collections": True,
                "document_metadata": True,
                "chunk_preview": True,
                "ingest_jobs": True,
                "query_audit": True,
                "citations": True,
            },
            "live_retrieval_verified": False,
            "note": "当前仅实现 RAG 接口与审计框架；未接入数据源、Embedding、向量库或重排模型。",
        }

    def _scenario(self, sid):
        self.rt.pack(sid)
        return sid

    def _collection(self, cid):
        with self.store.db() as c:
            row = c.execute("SELECT * FROM rag_collections WHERE id=?", (cid,)).fetchone()
        if not row:
            raise DomainError("RAG Collection 不存在", 404)
        return dict(row) | {"config": load(row["config"])}

    def collection(self, cid):
        return self._collection(cid)

    def collections(self, scenario=None):
        sql = "SELECT * FROM rag_collections"
        args = []
        if scenario:
            self._scenario(scenario)
            sql += " WHERE scenario=?"
            args.append(scenario)
        sql += " ORDER BY updated_at DESC"
        with self.store.db() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(row) | {"config": load(row["config"])} for row in rows]

    def create_collection(self, data):
        sid = str(data.get("scenario_id") or "").strip()
        cid = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        if not sid or not cid or not name:
            raise DomainError("RAG Collection 需要 scenario_id、id 和 name", 400)
        self._scenario(sid)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,99}", cid):
            raise DomainError("RAG Collection id 格式无效", 400)
        config = {
            "embedding": data.get("embedding") or {"provider": "none", "model": None, "dimensions": None},
            "vector_store": data.get("vector_store") or {"kind": "none"},
            "reranker": data.get("reranker") or {"kind": "none"},
            "chunking": data.get("chunking") or {"strategy": "character", "chunk_size": 800, "overlap": 120},
        }
        config = _clean_metadata(config)
        created = now()
        with self.store.db() as c:
            old = c.execute("SELECT * FROM rag_collections WHERE id=?", (cid,)).fetchone()
            if old:
                if old["scenario"] != sid or load(old["config"]) != config or old["name"] != name:
                    raise DomainError("RAG Collection id 已存在但配置不同", 409)
                return dict(old) | {"config": load(old["config"]), "reused": True}
            c.execute("INSERT INTO rag_collections VALUES (?,?,?,?,?,?,?,?)", (cid, sid, name, str(data.get("description") or "")[:2000], "draft", dump(config), created, created))
            self.store.event(c, sid, "rag.collection.created", cid, {"collection_id": cid, "status": "draft"})
        return self._collection(cid) | {"reused": False}

    def _check_collection_scenario(self, cid, sid=None):
        col = self._collection(cid)
        if sid and col["scenario"] != sid:
            raise DomainError("RAG Collection 不属于当前场景", 409)
        return col

    def documents(self, cid):
        self._collection(cid)
        with self.store.db() as c:
            rows = c.execute("SELECT * FROM rag_documents WHERE collection_id=? ORDER BY updated_at DESC", (cid,)).fetchall()
        return [dict(row) | {"metadata": load(row["metadata"])} for row in rows]

    def document(self, did):
        with self.store.db() as c:
            row = c.execute("SELECT * FROM rag_documents WHERE id=?", (did,)).fetchone()
        if not row:
            raise DomainError("RAG 文档不存在", 404)
        return dict(row) | {"metadata": load(row["metadata"])}

    def chunks(self, did):
        self.document(did)
        with self.store.db() as c:
            rows = c.execute("SELECT * FROM rag_chunks WHERE document_id=? ORDER BY ordinal", (did,)).fetchall()
        return [dict(row) | {"metadata": load(row["metadata"])} for row in rows]

    def register_document(self, cid, data):
        col = self._check_collection_scenario(cid)
        external_id = str(data.get("external_id") or "").strip()
        if not external_id or len(external_id) > 300:
            raise DomainError("RAG 文档需要 external_id", 400)
        checksum = str(data.get("checksum") or "").strip().lower()
        if checksum and not SHA256.fullmatch(checksum):
            raise DomainError("文档 checksum 必须是 64 位十六进制摘要", 400)
        metadata = _clean_metadata(data.get("metadata") or {})
        version = str(data.get("version") or "").strip()[:120]
        title = str(data.get("title") or "").strip()[:500]
        source_type = str(data.get("source_type") or "external-manifest").strip()[:100]
        source_ref = str(data.get("source_ref") or "").strip()[:1000]
        if SECRET_KEY.search(source_ref):
            raise DomainError("RAG source_ref 不得包含凭据", 400)
        with self.store.db() as c:
            old = c.execute("SELECT * FROM rag_documents WHERE collection_id=? AND external_id=? AND version=?", (cid, external_id, version)).fetchone()
            if old:
                if old["checksum"] != checksum or load(old["metadata"]) != metadata:
                    raise DomainError("相同 RAG 文档版本内容发生变化", 409)
                return dict(old) | {"metadata": load(old["metadata"]), "reused": True}
            did = uid("RAGDOC")
            stamp = now()
            c.execute("INSERT INTO rag_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (did, cid, external_id, title, source_type, source_ref, version, checksum, dump(metadata), "registered", stamp, stamp))
            self.store.event(c, col["scenario"], "rag.document.registered", did, {"collection_id": cid, "external_id": external_id, "version": version})
        with self.store.db() as c:
            row = c.execute("SELECT * FROM rag_documents WHERE id=?", (did,)).fetchone()
        return dict(row) | {"metadata": load(row["metadata"]), "reused": False}

    def jobs(self, cid):
        self._collection(cid)
        with self.store.db() as c:
            rows = c.execute("SELECT * FROM rag_jobs WHERE collection_id=? ORDER BY created_at DESC", (cid,)).fetchall()
        return [dict(row) | {"request": load(row["request"]), "result": load(row["result"])} for row in rows]

    def create_job(self, cid, data):
        col = self._collection(cid)
        kind = str(data.get("kind") or "ingest").strip()
        if kind not in ("ingest", "embed", "index", "rebuild"):
            raise DomainError("RAG job kind 无效", 400)
        mode = str(data.get("mode") or "plan").strip()
        if mode != "plan":
            raise DomainError("当前阶段只允许 mode=plan，不执行数据收集或索引", 409)
        request = _clean_metadata({"kind": kind, "mode": mode, "source": data.get("source") or {}, "options": data.get("options") or {}})
        jid = uid("RAGJOB")
        result = {"executed": False, "status": "planned", "reason": "当前阶段未接入数据源、Embedding 或向量库", "collection_id": cid}
        with self.store.db() as c:
            stamp = now()
            c.execute("INSERT INTO rag_jobs VALUES (?,?,?,?,?,?,?,?,?)", (jid, cid, kind, "planned", dump(request), dump(result), "", stamp, stamp))
            self.store.event(c, col["scenario"], "rag.job.planned", jid, {"collection_id": cid, "kind": kind})
        return {"id": jid, "collection_id": cid, "kind": kind, "status": "planned", "request": request, "result": result, "error": "", "created_at": stamp, "updated_at": stamp}

    def job(self, jid):
        with self.store.db() as c:
            row = c.execute("SELECT * FROM rag_jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise DomainError("RAG job 不存在", 404)
        return dict(row) | {"request": load(row["request"]), "result": load(row["result"])}

    def query(self, cid, data, scenario=None):
        col = self._check_collection_scenario(cid, scenario)
        query = str(data.get("query") or "").strip()
        if not query or len(query) > 12000:
            raise DomainError("RAG query 需要 1–12000 字符", 400)
        top_k = _limit(data.get("top_k"), 5, 50)
        filters = _clean_metadata(data.get("filters") or {})
        qid = uid("RAGQ")
        result = {
            "query_id": qid,
            "collection_id": cid,
            "status": "not_configured",
            "hits": [],
            "citations": [],
            "reason": "未配置 Embedding Provider 或 Vector Store；不会返回虚假召回",
            "live_retrieval_verified": False,
        }
        with self.store.db() as c:
            c.execute("INSERT INTO rag_queries VALUES (?,?,?,?,?,?,?,?)", (qid, cid, query, dump(filters), top_k, "not_configured", dump(result), now()))
            self.store.event(c, col["scenario"], "rag.query.completed", qid, {"collection_id": cid, "status": "not_configured", "top_k": top_k})
        return result

    def query_detail(self, qid, scenario=None):
        with self.store.db() as c:
            row = c.execute("SELECT * FROM rag_queries WHERE id=?", (qid,)).fetchone()
            if not row:
                raise DomainError("RAG query 不存在", 404)
            self._check_collection_scenario(row["collection_id"], scenario)
            result = load(row["result"])
            hits = [dict(x) | {"citation": load(x["citation"])} for x in c.execute("SELECT * FROM rag_hits WHERE query_id=? ORDER BY rank", (qid,)).fetchall()]
        result["hits"] = hits
        result["filters"] = load(row["filters"])
        result["query"] = row["query"]
        return result

    def citation(self, qid, chunk_id=None, scenario=None):
        detail = self.query_detail(qid, scenario)
        hits = [x for x in detail.get("hits", []) if not chunk_id or x["chunk_id"] == chunk_id]
        if not hits:
            raise DomainError("RAG 引用不存在；当前查询可能没有已配置召回结果", 404)
        return {"query_id": qid, "citations": [x["citation"] for x in hits], "status": detail["status"]}

    def preview_chunking(self, document_id, data):
        with self.store.db() as c:
            row = c.execute("SELECT * FROM rag_documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            raise DomainError("RAG 文档不存在", 404)
        result = preview_chunks(data.get("text", ""), data.get("chunk_size", 800), data.get("overlap", 120))
        result["document_id"] = document_id
        result["source_collection"] = row["collection_id"]
        return result
