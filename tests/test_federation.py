import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FederationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _dataset(self, name="D", policy="controlled"):
        return self.service.create(
            self.admin, "dataset", {"name": name, "access_policy": policy}
        )

    def _institution(self, name):
        return self.service.create(self.admin, "institution", {"name": name})

    def test_reconcile_local_ledger_by_grant_number(self):
        """Offline institution issues grants, then reconciles with master."""
        dataset = self._dataset()
        inst = self._institution("inst-a")
        results = self.service.reconcile(
            self.admin,
            inst["id"],
            [
                {
                    "grant_number": "GN-001",
                    "dataset_id": dataset["id"],
                    "recipient": "researcher-1",
                    "status": "active",
                    "starts_at": "2026-01-01",
                    "expires_at": "2099-01-01",
                },
                {
                    "grant_number": "GN-002",
                    "dataset_id": dataset["id"],
                    "recipient": "researcher-2",
                    "status": "issued",
                },
            ],
        )
        self.assertEqual([r["status"] for r in results], ["reconciled", "reconciled"])
        self.assertEqual(results[0]["entity"]["status"], "active")
        self.assertEqual(results[1]["entity"]["status"], "issued")
        # master now holds both grants keyed by grant_number
        self.assertIsNotNone(self.repo.find_grant_by_number("GN-001"))
        self.assertIsNotNone(self.repo.find_grant_by_number("GN-002"))

    def test_dataset_restrict_invalidates_non_activated_grants(self):
        """Restricting a dataset returns locally issued grants for review."""
        dataset = self._dataset()
        inst = self._institution("inst-a")
        results = self.service.reconcile(
            self.admin,
            inst["id"],
            [
                {"grant_number": "GN-001", "dataset_id": dataset["id"], "recipient": "r1", "status": "issued"},
                {"grant_number": "GN-002", "dataset_id": dataset["id"], "recipient": "r2", "status": "active"},
            ],
        )
        issued_id = results[0]["entity"]["id"]
        active_id = results[1]["entity"]["id"]
        self.service.transition(self.admin, dataset["id"], "restrict", {"reason": "review"})
        issued = self.service.get(issued_id)
        active = self.service.get(active_id)
        self.assertEqual(issued["status"], "returned_for_review")
        self.assertEqual(active["status"], "active")

    def test_change_deadline_invalidates_non_activated_grant(self):
        """Changing a grant's deadline before activation returns it for review."""
        dataset = self._dataset()
        inst = self._institution("inst-a")
        results = self.service.reconcile(
            self.admin,
            inst["id"],
            [{"grant_number": "GN-001", "dataset_id": dataset["id"], "recipient": "r1", "status": "issued"}],
        )
        grant_id = results[0]["entity"]["id"]
        updated = self.service.transition(
            self.admin, grant_id, "change_deadline", {"expires_at": "2027-01-01"}
        )
        self.assertEqual(updated["status"], "returned_for_review")
        self.assertEqual(updated["data"]["expires_at"], "2027-01-01")

    def test_change_deadline_keeps_active_grant_active(self):
        """Changing an active grant's deadline just updates the term."""
        dataset = self._dataset()
        inst = self._institution("inst-a")
        results = self.service.reconcile(
            self.admin,
            inst["id"],
            [{"grant_number": "GN-001", "dataset_id": dataset["id"], "recipient": "r1", "status": "active", "expires_at": "2099-01-01"}],
        )
        grant_id = results[0]["entity"]["id"]
        updated = self.service.transition(
            self.admin, grant_id, "change_deadline", {"expires_at": "2027-01-01"}
        )
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["data"]["expires_at"], "2027-01-01")

    def test_first_writer_wins_on_concurrent_reconciliation(self):
        """Two institutions reconciling the same grant_number: first to land wins."""
        dataset = self._dataset()
        inst_a = self._institution("inst-a")
        inst_b = self._institution("inst-b")
        payload = {
            "grant_number": "GN-SHARED",
            "dataset_id": dataset["id"],
            "recipient": "researcher-x",
            "status": "active",
            "expires_at": "2099-01-01",
        }
        first = self.service.reconcile(self.admin, inst_a["id"], [dict(payload, recipient="from-a")])
        second = self.service.reconcile(self.admin, inst_b["id"], [dict(payload, recipient="from-b")])
        self.assertEqual(first[0]["status"], "reconciled")
        self.assertEqual(second[0]["status"], "conflict")
        self.assertEqual(second[0]["winner"], first[0]["entity"]["id"])
        # only one grant exists for that grant_number
        self.assertEqual(len(self.repo.find_entities("grant", "grant_number", "GN-SHARED")), 1)

    def test_reconcile_idempotency_returns_same_batch(self):
        """Repeating a reconciliation with the same key does not duplicate grants."""
        dataset = self._dataset()
        inst = self._institution("inst-a")
        items = [{"grant_number": "GN-001", "dataset_id": dataset["id"], "recipient": "r1"}]
        first = self.service.reconcile(self.admin, inst["id"], items, idempotency_key="recon-1")
        second = self.service.reconcile(self.admin, inst["id"], items, idempotency_key="recon-1")
        self.assertEqual(first, second)
        self.assertEqual(len(self.repo.find_entities("grant", "grant_number", "GN-001")), 1)

    def test_migrate_institution_grants(self):
        """Merging institutions moves historical grants to the new institution."""
        dataset = self._dataset()
        source = self._institution("source-inst")
        target = self._institution("merged-inst")
        self.service.reconcile(
            self.admin,
            source["id"],
            [
                {"grant_number": "GN-001", "dataset_id": dataset["id"], "recipient": "r1"},
                {"grant_number": "GN-002", "dataset_id": dataset["id"], "recipient": "r2"},
            ],
        )
        result = self.service.migrate_institution(self.admin, source["id"], target["id"])
        self.assertEqual(result["migrated"], 2)
        self.assertEqual(self.repo.list_grants_by_institution(source["id"]), [])
        self.assertEqual(len(self.repo.list_grants_by_institution(target["id"])), 2)

    def test_migrate_resumes_and_is_idempotent(self):
        """Re-running migration after interruption does not create duplicates."""
        dataset = self._dataset()
        source = self._institution("source-inst")
        target = self._institution("merged-inst")
        self.service.reconcile(
            self.admin,
            source["id"],
            [
                {"grant_number": "GN-001", "dataset_id": dataset["id"], "recipient": "r1"},
                {"grant_number": "GN-002", "dataset_id": dataset["id"], "recipient": "r2"},
                {"grant_number": "GN-003", "dataset_id": dataset["id"], "recipient": "r3"},
            ],
        )
        first = self.service.migrate_institution(
            self.admin, source["id"], target["id"], idempotency_key="mig-1", batch_size=1
        )
        self.assertEqual(first["migrated"], 3)
        # re-run with the same idempotency key returns the stored result
        second = self.service.migrate_institution(
            self.admin, source["id"], target["id"], idempotency_key="mig-1", batch_size=1
        )
        self.assertEqual(second, first)
        # re-run without idempotency key: nothing left to migrate, no duplicates
        third = self.service.migrate_institution(self.admin, source["id"], target["id"], batch_size=1)
        self.assertEqual(third["migrated"], 0)
        self.assertEqual(len(self.repo.list_grants_by_institution(target["id"])), 3)

    def test_migrate_rejects_same_institution(self):
        inst = self._institution("inst-a")
        with self.assertRaises(ValidationError):
            self.service.migrate_institution(self.admin, inst["id"], inst["id"])


if __name__ == "__main__":
    unittest.main()
