"""联盟授权账：主账对账与机构合并。"""

from .domain import Actor, ConflictError, NotFoundError
from .rules import GRANT_PENDING_STATES


SYSTEM_ACTOR = Actor("federation", "committee")


class FederationService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules

    def _rule_engine(self):
        if self.rules is None:
            from .rules import RuleEngine

            self.rules = RuleEngine()
        return self.rules

    def _record(self, institution_id, grant_id, action, outcome, detail=None):
        self.repository.append_reconciliation(
            institution_id, grant_id, action, outcome, detail or {}
        )

    def reconcile_grant(self, local_ledger, local_grant, pending_ops=None):
        """把一张本地授权按授权编号对到联盟主账。

        并发两家同时提交同一张授权时，归属认领在主账库上原子完成，
        只有先落主账的那家生效，后者收到 ConflictError。
        """
        repo = self.repository
        rules = self._rule_engine()
        institution_id = local_ledger.institution_id
        grant_id = local_grant["grant_id"]

        master = repo.get_entity(grant_id)
        if not master or master["kind"] != "grant":
            raise NotFoundError("grant unknown to master ledger: " + grant_id)

        claim = repo.claim_grant_ownership(grant_id, institution_id)
        if claim == "already_owned":
            self._record(institution_id, grant_id, "reconcile", "already_owned")

        dataset = (repo.get_entity(master["data"].get("dataset_id"))
                   if master["data"].get("dataset_id") else None)

        blocked = (
            dataset is not None and dataset["status"] == "restricted"
        ) or (
            int(master["data"].get("policy_version", 1))
            > int(local_grant.get("policy_version", 1))
        )
        if blocked and local_grant["status"] in GRANT_PENDING_STATES:
            # 主账限制数据集或改期限后，本地没启用的授权立刻失效并退回复核。
            reason = ("dataset restricted by master"
                      if dataset is not None and dataset["status"] == "restricted"
                      else "master policy updated")
            updated = self._apply_master_action(
                grant_id, "invalidate", {"reason": reason}
            )
            local_ledger.sync_grant(
                grant_id, updated["status"],
                int(updated["data"].get("policy_version", 1)),
            )
            self._record(
                institution_id, grant_id, "invalidate", "invalidated",
                {"reason": reason},
            )
            for op in pending_ops or []:
                local_ledger.mark_op(op["op_id"], "rejected")
                self._record(
                    institution_id, grant_id, op["op_type"], "rejected",
                    {"reason": "grant invalidated by master policy"},
                )
            return updated

        ops = pending_ops if pending_ops is not None else local_ledger.list_pending_ops()
        ops = [op for op in ops if op["grant_id"] == grant_id]
        # 先取用后撤回，按办理顺序重放到主账
        ops.sort(key=lambda op: (0 if op["op_type"] == "activate" else 1,
                                 op["created_at"]))
        result = master
        for op in ops:
            try:
                if op["op_type"] == "activate" and result["status"] == "issued":
                    result = self._apply_master_action(
                        grant_id, "activate", op["payload"]
                    )
                elif op["op_type"] == "revoke" and result["status"] in ("issued", "active"):
                    result = self._apply_master_action(
                        grant_id, "revoke", op["payload"]
                    )
                else:
                    # 主账已处于目标状态：断网重连的重复办理按幂等处理。
                    local_ledger.mark_op(op["op_id"], "already_applied")
                    self._record(
                        institution_id, grant_id, op["op_type"], "already_applied"
                    )
                    continue
                local_ledger.mark_op(op["op_id"], "reconciled")
                self._record(institution_id, grant_id, op["op_type"], "reconciled")
            except ConflictError:
                local_ledger.mark_op(op["op_id"], "stale_version")
                self._record(institution_id, grant_id, op["op_type"], "stale_version")

        local_ledger.sync_grant(
            grant_id, result["status"], int(result["data"].get("policy_version", 1))
        )
        self._record(institution_id, grant_id, "reconcile", "synced")
        return result

    def reconcile_ledger(self, local_ledger):
        """对整份本地账对账，逐张授权返回结果。"""
        results = []
        pending = local_ledger.list_pending_ops()
        for local_grant in local_ledger.list_grants():
            results.append(
                self.reconcile_grant(local_ledger, local_grant, pending)
            )
        return results

    def _apply_master_action(self, grant_id, action, data):
        """用系统机构身份驱动主账状态机（权限与状态校验仍走规则引擎）。"""
        from .service import DomainService

        service = DomainService(self.repository, self._rule_engine())
        return service.transition(SYSTEM_ACTOR, grant_id, action, dict(data or {}))
