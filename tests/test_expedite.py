"""Shortage expedite action and Mock ERP MCP business-state tests."""
import tempfile
import unittest
from pathlib import Path

from app.engine import DomainError
from app.runtime import Runtime
from mcp_servers.mock_erp_service import ExpediteStore


ROOT = Path(__file__).resolve().parents[1]


class ExpediteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ExpediteStore(Path(self.temp.name) / "erp.sqlite3", ttl_seconds=60)
        self.payload = {
            "idempotency_key": "TASK-1:RUN-1:NODE-1:V1",
            "material_id": "MAT-2103",
            "material_name": "精密丝杠副",
            "supplier": "启明精工",
            "shortage_qty": 18,
            "due_days": 2,
            "expected_object_version": "record-v1",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_commit_and_status_are_idempotent(self):
        prepared = self.store.prepare(**self.payload)
        repeated = self.store.prepare(**self.payload)
        self.assertEqual(prepared["id"], repeated["id"])
        self.assertTrue(repeated["reused"])
        committed = self.store.commit(prepared["prepare_token"], "record-v1")
        self.assertEqual(committed["status"], "committed")
        self.assertTrue(committed["external_ref"].startswith("EXP-"))
        retried = self.store.commit(prepared["prepare_token"], "record-v1")
        self.assertEqual(committed["external_ref"], retried["external_ref"])
        self.assertTrue(retried["reused"])
        observed = self.store.status(idempotency_key=self.payload["idempotency_key"])
        self.assertEqual(observed["status"], "committed")

    def test_idempotency_conflict_and_version_change_are_rejected(self):
        prepared = self.store.prepare(**self.payload)
        changed = {**self.payload, "shortage_qty": 19}
        with self.assertRaisesRegex(ValueError, "different payload"):
            self.store.prepare(**changed)
        with self.assertRaisesRegex(ValueError, "changed after approval"):
            self.store.commit(prepared["prepare_token"], "record-v2")
        self.assertEqual(
            self.store.status(operation_id=prepared["id"])["status"], "prepared"
        )

    def test_prepared_operation_can_be_rolled_back(self):
        prepared = self.store.prepare(**self.payload)
        rolled_back = self.store.rollback(prepared["prepare_token"])
        self.assertEqual(rolled_back["status"], "rolled_back")
        self.assertTrue(self.store.rollback(prepared["prepare_token"])["reused"])


class MaterialsExpediteActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Path(self.temp.name) / "workbench.sqlite3", ROOT / "packs")
        self.case = next(
            item for item in self.runtime.cases("materials") if item["severity"] == "high"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_high_risk_case_builds_explicit_mcp_payload(self):
        action = self.runtime.prepare_action(
            "materials", "erp.expedite", [self.case["id"]],
            idempotency_key="draft-1",
        )
        item = action["messages"][0]
        self.assertEqual(action["capability"], "erp.expedite")
        self.assertEqual(item["payload"]["material_id"], self.case["object_id"])
        self.assertGreater(item["payload"]["shortage_qty"], 0)
        self.assertEqual(item["payload"]["supplier"], item["recipient"])
        self.assertEqual(
            item["payload"]["expected_object_version"],
            action["record_fingerprints"][self.case["object_id"]],
        )
        self.assertEqual(
            action["id"],
            self.runtime.prepare_action(
                "materials", "erp.expedite", [self.case["id"]],
                idempotency_key="draft-1",
            )["id"],
        )

    def test_confirmed_action_is_delegated_once_to_dsh(self):
        class AgentStub:
            def __init__(self):
                self.requests = []

            def submit(self, request):
                self.requests.append(request)
                return {"id": "TASK-EXPEDITE-1"}

        agent = AgentStub()
        self.runtime.agent = agent
        action = self.runtime.prepare_action(
            "materials", "erp.expedite", [self.case["id"]],
            idempotency_key="draft-2",
        )
        executed = self.runtime.execute_action(action["id"], action["draft_revision"])
        self.assertEqual(executed["status"], "executing")
        self.assertEqual(executed["agent_task_id"], "TASK-EXPEDITE-1")
        self.assertEqual(len(agent.requests), 1)
        self.assertIn("prepare_expedite", agent.requests[0]["message"])
        self.assertIn(action["messages"][0]["payload"]["idempotency_key"], agent.requests[0]["message"])

    def test_structured_supplier_cannot_be_changed_in_text_editor(self):
        action = self.runtime.prepare_action(
            "materials", "erp.expedite", [self.case["id"]],
            idempotency_key="draft-target",
        )
        changed = [{**action["messages"][0], "recipient": "another-supplier"}]
        with self.assertRaisesRegex(DomainError, "供应商来自业务数据"):
            self.runtime.edit_action(action["id"], {"messages": changed})

    def test_changed_business_data_invalidates_confirmation(self):
        action = self.runtime.prepare_action(
            "materials", "erp.expedite", [self.case["id"]],
            idempotency_key="draft-3",
        )
        record = next(
            row for row in self.runtime.raw_records("materials")
            if row["id"] == self.case["object_id"]
        )
        self.runtime.ingest("materials", [{**record, "stock": record["stock"] + 1}])
        self.runtime.agent = type("AgentStub", (), {"submit": lambda *_: {"id": "unused"}})()
        with self.assertRaisesRegex(DomainError, "数据已变化"):
            self.runtime.execute_action(action["id"], action["draft_revision"])


if __name__ == "__main__":
    unittest.main()
