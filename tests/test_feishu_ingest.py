"""Scheduled Feishu CLI manifest contract; no Feishu network calls."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine import DomainError
from app.runtime import Runtime


class FeishuIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rt = Runtime(Path(self.temp.name) / "workbench.sqlite3", ROOT / "packs")
        self.sid = "materials"
        self.record = copy.deepcopy(self.rt.raw_records(self.sid)[0])

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_is_durable_and_idempotent(self):
        body = {
            "event_id": "feishu-job-001",
            "pulled_at": "2026-09-20T08:00:00+08:00",
            "source": {"provider": "feishu-cli", "job_id": "scheduled-1"},
            "artifacts": [{
                "artifact_id": "img-001",
                "kind": "image",
                "relative_path": "data/integrations/feishu/materials/img-001.png",
                "sha256": "a" * 64,
            }, {
                "artifact_id": "img-002",
                "kind": "document",
                "relative_path": "data/integrations/feishu/materials/img-002.pdf",
            }],
            "records": [self.record],
            "mode": "upsert",
        }
        first = self.rt.ingest_feishu_batch(self.sid, body)
        second = self.rt.ingest_feishu_batch(self.sid, body)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        listed = self.rt.feishu_artifacts(self.sid)
        self.assertEqual(len(listed["batches"]), 1)
        self.assertEqual(len(listed["artifacts"]), 2)
        self.assertEqual(listed["artifacts"][0]["artifact_id"], "img-001")
        self.assertEqual(first["data_result"]["event_key"], second["data_result"]["event_key"])

    def test_manifest_rejects_secret_and_escape_path(self):
        base = {"event_id": "feishu-job-002", "pulled_at": "2026-09-20T08:00:00+08:00", "artifacts": []}
        secret = dict(base, artifacts=[{"artifact_id": "bad", "uri": "https://x/?access_token=secret"}])
        with self.assertRaises(DomainError):
            self.rt.ingest_feishu_batch(self.sid, secret)
        metadata_secret = dict(base, artifacts=[{"artifact_id": "bad-meta", "metadata": {"tenant_access_token": "secret"}}])
        with self.assertRaises(DomainError):
            self.rt.ingest_feishu_batch(self.sid, metadata_secret)
        escape = dict(base, artifacts=[{"artifact_id": "bad", "relative_path": "data/../secrets.txt"}])
        with self.assertRaises(DomainError):
            self.rt.ingest_feishu_batch(self.sid, escape)

    def test_artifact_change_under_same_id_is_rejected(self):
        body = {"event_id": "feishu-job-003", "pulled_at": "2026-09-20T08:00:00+08:00", "artifacts": [{"artifact_id": "img-003", "kind": "image"}]}
        self.rt.ingest_feishu_batch(self.sid, body)
        changed = {**body, "event_id": "feishu-job-004", "artifacts": [{"artifact_id": "img-003", "kind": "document"}]}
        with self.assertRaises(DomainError):
            self.rt.ingest_feishu_batch(self.sid, changed)


if __name__ == "__main__":
    unittest.main()
