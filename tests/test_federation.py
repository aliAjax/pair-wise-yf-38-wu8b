import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.federation import FederationService
from src.local_ledger import LocalLedgerStore
from src.merger import InstitutionMerger
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


ADMIN = Actor("admin", "admin")


class FederationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = SQLiteRepository(root / "master.db")
        self.rules = RuleEngine()
        self.service = DomainService(self.repo, self.rules)
        self.federation = FederationService(self.repo, self.rules)
        self.ledgers = LocalLedgerStore(root / "ledgers")
        self.merger = InstitutionMerger(self.repo, self.ledgers)
        self.grant_id = self._issue_grant()

    def tearDown(self):
        self.tmp.cleanup()

    def _issue_grant(self):
        dataset = self.service.create(
            ADMIN, "dataset",
            {"name": "Cohort", "access_policy": "controlled"},
        )
        application = self.service.create(
            ADMIN, "application",
            {"dataset_id": dataset["id"], "applicant_id": "A", "purpose": "analysis"},
        )
        grant = self.service.create(
            ADMIN, "grant",
            {"application_id": application["id"],
             "dataset_id": dataset["id"], "recipient": "r-1"},
        )
        return grant["id"]

    def _dataset_id(self, grant_id):
        return self.repo.get_entity(grant_id)["data"]["dataset_id"]

    def test_offline_operations_reconcile_by_grant_id(self):
        ledger = self.ledgers.get("org-a")
        ledger.register_grant(self.grant_id, policy_version=1)
        # 断网期间办理取用与撤回
        ledger.activate(self.grant_id, "2026-10-01", "2099-01-01")
        ledger.revoke(self.grant_id, "purpose changed offline")

        result = self.federation.reconcile_ledger(ledger)
        master = result[0]
        self.assertEqual(master["status"], "revoked")
        local = ledger.get_grant(self.grant_id)
        self.assertEqual(local["status"], "revoked")
        self.assertEqual(ledger.list_pending_ops(), [])
        self.assertEqual(self.repo.get_owner(self.grant_id), "org-a")

        # 网络再次抖动后重复对账是幂等的，不会多出操作
        again = self.federation.reconcile_ledger(ledger)
        self.assertEqual(again[0]["status"], "revoked")

    def test_master_restriction_invalidates_unactivated_local_grant(self):
        ledger = self.ledgers.get("org-b")
        ledger.register_grant(self.grant_id, policy_version=1)
        ledger.activate(self.grant_id, "2026-10-01", "2099-01-01")

        # 主账限制数据集
        self.service.transition(
            ADMIN, self._dataset_id(self.grant_id),
            "restrict", {"reason": "new governance"},
        )

        master = self.federation.reconcile_grant(
            ledger, ledger.get_grant(self.grant_id), ledger.list_pending_ops()
        )
        self.assertEqual(master["status"], "review_returned")
        self.assertEqual(ledger.get_grant(self.grant_id)["status"], "review_returned")
        self.assertEqual(
            [op["state"] for op in ledger.list_ops()], ["rejected"]
        )

    def test_master_term_change_invalidates_unactivated_grant(self):
        ledger = self.ledgers.get("org-b2")
        ledger.register_grant(self.grant_id, policy_version=1)

        self.service.transition(
            ADMIN, self.grant_id,
            "change_term", {"expires_at": "2026-12-31"},
        )
        master = self.federation.reconcile_ledger(ledger)
        self.assertEqual(master[0]["status"], "review_returned")
        self.assertGreater(master[0]["data"]["policy_version"], 1)

    def test_two_institutions_reconciling_same_grant_first_writer_wins(self):
        ledger_a = self.ledgers.get("org-x")
        ledger_b = self.ledgers.get("org-y")
        ledger_a.register_grant(self.grant_id, policy_version=1)
        ledger_b.register_grant(self.grant_id, policy_version=1)

        barrier = threading.Barrier(2)
        outcomes = {}

        def run(name, ledger):
            barrier.wait()
            try:
                self.federation.reconcile_ledger(ledger)
                outcomes[name] = "ok"
            except ConflictError:
                outcomes[name] = "conflict"

        t1 = threading.Thread(target=run, args=("a", ledger_a))
        t2 = threading.Thread(target=run, args=("b", ledger_b))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(sorted(outcomes.values()), ["conflict", "ok"])
        # 主账上只落一家归属，只有一份授权
        owner = self.repo.get_owner(self.grant_id)
        self.assertIn(owner, ("org-x", "org-y"))
        self.assertEqual(len(self.repo.list_entities(kind="grant")), 1)

    def test_merge_resumable_and_idempotent(self):
        # 机构 org-old 先正常对账持有授权
        old_ledger = self.ledgers.get("org-old")
        old_ledger.register_grant(self.grant_id, policy_version=1)
        self.federation.reconcile_ledger(old_ledger)

        merge = self.merger.start_merge(
            ADMIN, "org-old", "org-new", merge_id="merge-1"
        )
        self.assertEqual(merge["total"], 1)

        # 迁移中断：处理 0 张后接着做
        partial, migrated_zero = self.merger.run_merge(ADMIN, "merge-1", limit=0)
        self.assertEqual(migrated_zero, 0)
        self.assertNotEqual(partial["status"], "completed")
        self.assertEqual(partial["processed"], 0)

        # 重复提交同一份迁移不会产生第二份
        with self.assertRaises(ConflictError):
            self.merger.start_merge(
                ADMIN, "org-old", "org-new", merge_id="merge-1"
            )

        # 断点续做
        done, migrated = self.merger.run_merge(ADMIN, "merge-1")
        self.assertEqual(migrated, 1)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.repo.get_owner(self.grant_id), "org-new")

        # 新机构本地账已迁入且只有一份
        new_ledger = self.ledgers.get("org-new")
        self.assertEqual(len(new_ledger.list_grants()), 1)
        self.assertEqual(new_ledger.get_grant(self.grant_id)["grant_id"], self.grant_id)

        # 再跑一次没有重复授权
        self.merger.run_merge(ADMIN, "merge-1")
        self.assertEqual(len(self.repo.list_entities(kind="grant")), 1)
        self.assertEqual(len(new_ledger.list_grants()), 1)


if __name__ == "__main__":
    unittest.main()
