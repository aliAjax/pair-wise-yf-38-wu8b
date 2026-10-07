"""机构合并：把历史数据里的授权迁移到新机构。

迁移按授权逐张落检查点，中断后用同一 merge_id 再次 run 可接着做；
merge_id 唯一，重复提交同一份迁移不会多出第二份授权。
"""

from uuid import uuid4

from .domain import ConflictError, NotFoundError, PermissionDenied
from .federation import SYSTEM_ACTOR
from .rules import FEDERATION_ADMINS


class InstitutionMerger:
    def __init__(self, repository, local_ledger_store):
        self.repository = repository
        # local_ledger_store: callable(institution_id) -> LocalLedger
        self.local_ledger_store = local_ledger_store

    @staticmethod
    def _authorize(actor):
        if actor.role not in FEDERATION_ADMINS:
            raise PermissionDenied(
                "role %s is not allowed to merge institutions" % actor.role
            )

    def start_merge(self, actor, from_institution, to_institution, merge_id=None):
        self._authorize(actor)
        if from_institution == to_institution:
            raise ConflictError("cannot merge an institution into itself")
        merge_id = merge_id or str(uuid4())

        grant_ids = set(self.repository.list_owned_grants(from_institution))
        source_ledger = self.local_ledger_store(from_institution)
        if source_ledger is not None:
            for grant in source_ledger.list_grants():
                grant_ids.add(grant["grant_id"])
        return self.repository.create_merge(
            merge_id, from_institution, to_institution, sorted(grant_ids)
        )

    def run_merge(self, actor, merge_id, limit=None):
        """迁移一批授权；任何阶段中断后重跑只处理仍是 pending 的授权。"""
        self._authorize(actor)
        merge = self.repository.get_merge(merge_id)
        if not merge:
            raise NotFoundError("merge not found: " + merge_id)

        source = self.local_ledger_store(merge["from_institution"])
        target = self.local_ledger_store(merge["to_institution"])
        pending = self.repository.list_merge_items(merge_id, state="pending")
        if limit is not None:
            pending = pending[: int(limit)]

        migrated = 0
        for item in pending:
            grant_id = item["grant_id"]
            self._migrate_one(merge, grant_id, source, target)
            self.repository.mark_merge_item(merge_id, grant_id, "migrated")
            migrated += 1
        return self.repository.get_merge(merge_id), migrated

    def _migrate_one(self, merge, grant_id, source, target):
        repo = self.repository
        to_institution = merge["to_institution"]

        master = repo.get_entity(grant_id)
        if master is None:
            repo.mark_merge_item(merge["merge_id"], grant_id, "missing_master")
            return

        owner = repo.get_owner(grant_id)
        if owner is None:
            # 主账有授权但未登记归属：直接认领给新机构。
            repo.claim_grant_ownership(grant_id, to_institution)
        elif owner == merge["from_institution"]:
            repo.transfer_ownership(grant_id, to_institution)
        elif owner != to_institution:
            repo.mark_merge_item(merge["merge_id"], grant_id, "owned_by_other")
            return

        if target is not None:
            local_grant = target.get_grant(grant_id)
            if local_grant is None:
                if source is not None:
                    source_grant = source.get_grant(grant_id)
                    if source_grant is not None:
                        target.register_grant(
                            grant_id,
                            data=source_grant["data"],
                            policy_version=source_grant["policy_version"],
                        )
                        # 新机构本地账以主账当前状态为准
                        target.sync_grant(
                            grant_id,
                            master["status"],
                            int(master["data"].get("policy_version", 1)),
                        )

        repo.append_audit(
            grant_id,
            SYSTEM_ACTOR.user_id,
            SYSTEM_ACTOR.role,
            "merge_transfer",
            merge["from_institution"],
            master["status"],
            {"merge_id": merge["merge_id"], "to_institution": to_institution},
        )
