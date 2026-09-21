"""RAG framework contracts. No data source, model, embedding or vector DB call."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from app import bridge
from app.agent import AgentService
from app.engine import DomainError
from app.rag import preview_chunks
from app.runtime import Runtime
from app.store import dump, now


ROOT = Path(__file__).resolve().parents[1]


class RAGFrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rt = Runtime(Path(self.temp.name) / "rag.sqlite3", ROOT / "packs")
        self.rag = self.rt.rag
        self.collection = self.rag.create_collection({
            "scenario_id": "materials",
            "id": "supply-chain-evidence",
            "name": "供应链证据库",
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_capabilities_are_explicitly_unconfigured(self):
        info = self.rag.capabilities()
        self.assertEqual(info["mode"], "contract-only")
        self.assertFalse(info["embedding"]["configured"])
        self.assertFalse(info["vector_store"]["configured"])
        self.assertFalse(info["live_retrieval_verified"])
        with self.assertRaises(DomainError):
            self.rag.embedding.embed(["no network call"])

    def test_collection_is_idempotent_but_conflict_is_rejected(self):
        reused = self.rag.create_collection({
            "scenario_id": "materials", "id": "supply-chain-evidence", "name": "供应链证据库"
        })
        self.assertTrue(reused["reused"])
        with self.assertRaises(DomainError):
            self.rag.create_collection({
                "scenario_id": "materials", "id": "supply-chain-evidence", "name": "不同名字"
            })

    def test_document_metadata_version_and_secret_rules(self):
        checksum = hashlib.sha256(b"fixture").hexdigest()
        data = {
            "external_id": "supplier-contract-001",
            "title": "供应商协议",
            "source_type": "scheduled_feishu_artifact",
            "source_ref": "artifact-001",
            "version": "2026-09-21",
            "checksum": checksum,
            "metadata": {"supplier": "脱敏供应商", "department": "采购"},
        }
        first = self.rag.register_document(self.collection["id"], data)
        second = self.rag.register_document(self.collection["id"], data)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        with self.assertRaises(DomainError):
            self.rag.register_document(self.collection["id"], {**data, "checksum": "b" * 64})
        with self.assertRaises(DomainError):
            self.rag.register_document(self.collection["id"], {
                **data, "external_id": "secret-doc", "metadata": {"tenant_access_token": "secret"}
            })

    def test_chunk_preview_is_deterministic_and_not_persisted(self):
        document = self.rag.register_document(self.collection["id"], {"external_id": "doc-for-chunks"})
        first = self.rag.preview_chunking(document["id"], {"text": "A" * 260, "chunk_size": 120, "overlap": 20})
        second = preview_chunks("A" * 260, 120, 20)
        self.assertFalse(first["persisted"])
        self.assertEqual([x["checksum"] for x in first["chunks"]], [x["checksum"] for x in second["chunks"]])
        with self.rt.store.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM rag_chunks").fetchone()[0], 0)

    def test_job_is_plan_only(self):
        job = self.rag.create_job(self.collection["id"], {"kind": "ingest", "mode": "plan", "source": {"type": "future-manifest"}})
        self.assertEqual(job["status"], "planned")
        self.assertFalse(job["result"]["executed"])
        self.assertEqual(self.rag.job(job["id"])["id"], job["id"])
        with self.assertRaises(DomainError):
            self.rag.create_job(self.collection["id"], {"kind": "ingest", "mode": "execute"})

    def test_query_is_audited_without_fake_hits(self):
        result = self.rag.query(self.collection["id"], {
            "query": "供应商延期交付时的升级流程是什么？", "top_k": 5, "filters": {"supplier": "脱敏供应商"}
        })
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["citations"], [])
        self.assertFalse(result["live_retrieval_verified"])
        detail = self.rag.query_detail(result["query_id"])
        self.assertEqual(detail["query"], "供应商延期交付时的升级流程是什么？")
        with self.assertRaises(DomainError):
            self.rag.citation(result["query_id"])

    def test_bridge_scopes_search_to_current_scenario(self):
        other = self.rag.create_collection({"scenario_id": "delivery", "id": "delivery-evidence", "name": "交付证据库"})
        agent = AgentService(self.rt, poll_interval=99)
        task = {
            "id": "RAG-BRIDGE-TASK", "scenario": "materials", "session_id": "SESSION-RAG",
            "status": "running", "mode": "dsh", "revision": 1, "created_at": now(), "updated_at": now(),
            "message": "fixture", "activity": [], "outputs": [], "handle": None,
            "stop_requested": False, "_bridge_token": "rag-token", "context": {},
        }
        with self.rt.store.db() as connection:
            connection.execute("INSERT INTO sessions VALUES (?,?,?)", (task["session_id"], "materials", now()))
            connection.execute("INSERT INTO tasks VALUES (?,?,?,?,?)", (task["id"], "materials", task["session_id"], dump(task), now()))
        try:
            ok = bridge.invoke(agent, task["id"], "rag-token", {
                "operation": "rag.search", "parameters": {"collection_id": self.collection["id"], "query": "fixture"}
            })
            self.assertEqual(ok["status"], "not_configured")
            with self.assertRaises(DomainError):
                bridge.invoke(agent, task["id"], "rag-token", {
                    "operation": "rag.search", "parameters": {"collection_id": other["id"], "query": "cross scenario"}
                })
        finally:
            agent.shutdown()


if __name__ == "__main__":
    unittest.main()
