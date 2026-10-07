from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        if kind == "grant" and not payload.get("grant_number"):
            payload["grant_number"] = "GN-" + uuid4().hex[:12]
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        if entity["kind"] == "grant" and action == "change_deadline":
            return self._change_grant_deadline(actor, entity, data, expected)
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "dataset" and action == "restrict":
            self._invalidate_issued_grants(actor, entity)
        return updated

    def _change_grant_deadline(self, actor, entity, data, expected_version):
        new_expires = self.rules.validate_change_deadline(actor, entity, data, self._lookup)
        merged = dict(entity["data"])
        merged["expires_at"] = new_expires
        next_status = "returned_for_review" if entity["status"] == "issued" else entity["status"]
        updated = self.repository.update_entity(entity["id"], expected_version, next_status, merged)
        self.audit.record(
            entity["id"],
            actor,
            "change_deadline",
            entity["status"],
            updated["status"],
            {"expires_at": new_expires},
        )
        return updated

    def _invalidate_issued_grants(self, actor, dataset):
        for grant in self.rules.affected_by_restrict(dataset, self._lookup):
            try:
                self.transition(actor, grant["id"], "invalidate", {"reason": "dataset restricted"})
            except (ConflictError, ValidationError):
                continue

    def reconcile(self, actor, institution_id, items, idempotency_key=None):
        """Reconcile an institution's local grant ledger with the alliance master.

        Each item is keyed by grant_number. The first reconciliation to land in
        the master wins; later submissions for the same grant_number are reported
        as conflicts rather than overwriting the earlier record.
        """
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                batch = self.repository.get_entity(existing)
                if batch and batch["kind"] == "reconciliation_batch":
                    return batch["data"]["results"]
        institution = self.repository.get_entity(institution_id)
        if not institution or institution["kind"] != "institution":
            raise NotFoundError("institution not found: " + str(institution_id))
        if not isinstance(items, list) or not items:
            raise ValidationError("reconciliation items are required")
        results = []
        for item in items:
            results.append(self._reconcile_one(actor, institution_id, item))
        if idempotency_key:
            batch_id = str(uuid4())
            batch = self.repository.create_entity(
                batch_id,
                "reconciliation_batch",
                "done",
                {"institution_id": institution_id, "results": results},
                actor.user_id,
            )
            self.repository.save_idempotency(actor.user_id, idempotency_key, batch_id)
        return results

    def _reconcile_one(self, actor, institution_id, item):
        grant_number = item.get("grant_number")
        if not grant_number:
            raise ValidationError("grant_number is required for reconciliation")
        existing = self.repository.find_grant_by_number(grant_number)
        if existing:
            return {
                "grant_number": grant_number,
                "status": "conflict",
                "winner": existing["id"],
            }
        status = item.get("status", "issued")
        if status not in ("issued", "active", "revoked", "expired", "returned_for_review"):
            status = "issued"
        data = {
            "grant_number": grant_number,
            "dataset_id": item["dataset_id"],
            "application_id": item.get("application_id"),
            "recipient": item["recipient"],
            "institution_id": institution_id,
            "starts_at": item.get("starts_at"),
            "expires_at": item.get("expires_at"),
            "reconciled": True,
        }
        try:
            entity = self.repository.create_entity(
                str(uuid4()), "grant", status, data, actor.user_id
            )
        except ConflictError:
            winner = self.repository.find_grant_by_number(grant_number)
            return {
                "grant_number": grant_number,
                "status": "conflict",
                "winner": winner["id"] if winner else None,
            }
        return {"grant_number": grant_number, "status": "reconciled", "entity": entity}

    def migrate_institution(self, actor, source_id, target_id, idempotency_key=None, batch_size=100):
        """Migrate an institution's historical grants into a merged institution.

        Grants are reassigned from source to target. Re-running after an
        interruption resumes from the first unmigrated grant; grants already
        moved have the target institution_id and are skipped, so a repeated
        submission never creates a second copy.
        """
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                batch = self.repository.get_entity(existing)
                if batch and batch["kind"] == "migration_batch":
                    return batch["data"]["result"]
        source = self.repository.get_entity(source_id)
        target = self.repository.get_entity(target_id)
        if not source or source["kind"] != "institution":
            raise NotFoundError("source institution not found: " + str(source_id))
        if not target or target["kind"] != "institution":
            raise NotFoundError("target institution not found: " + str(target_id))
        if source_id == target_id:
            raise ValidationError("source and target institutions must differ")
        migrated = 0
        cursor = None
        while True:
            batch = self.repository.list_grants_by_institution(
                source_id, limit=batch_size, cursor=cursor
            )
            if not batch:
                break
            for grant in batch:
                if grant["data"].get("institution_id") == target_id:
                    continue
                merged = dict(grant["data"])
                merged["institution_id"] = target_id
                self.repository.update_entity(grant["id"], None, grant["status"], merged)
                migrated += 1
            cursor = batch[-1]["id"]
        result = {"source": source_id, "target": target_id, "migrated": migrated}
        if idempotency_key:
            batch_id = str(uuid4())
            batch_entity = self.repository.create_entity(
                batch_id, "migration_batch", "done", {"result": result}, actor.user_id
            )
            self.repository.save_idempotency(actor.user_id, idempotency_key, batch_id)
        return result

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
