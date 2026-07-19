"""P1 email-identical identity dedup (RFC customer-identity §5–6)."""

from __future__ import annotations

import uuid
from typing import Any, Protocol


ADVISORY_LOCK_KEY = 0x50490001  # P1 identity email dedup


class IdentityEmailDedupStore(Protocol):
    def find_duplicate_email_groups(self, *, limit: int = 0) -> list[dict[str, Any]]: ...

    def merge_email_duplicate_group(
        self,
        *,
        email_norm: str,
        canonical_identity_id: str,
        duplicate_identity_ids: list[str],
        operator_id: str = "system",
    ) -> dict[str, Any]: ...

    def count_duplicate_email_groups(self) -> int: ...


def _new_log_id() -> str:
    return f"iml_{uuid.uuid4().hex[:16]}"


def plan_email_dedup_groups(
    groups: list[dict[str, Any]],
    *,
    limit: int = 0,
) -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
    for group in groups:
        ids = [str(x).strip() for x in (group.get("identity_ids") or []) if str(x).strip()]
        if len(ids) < 2:
            continue
        planned.append(
            {
                "email_norm": str(group.get("email_norm") or "").strip().lower(),
                "identity_count": len(ids),
                "canonical_identity_id": ids[0],
                "duplicate_identity_ids": ids[1:],
            }
        )
        if limit > 0 and len(planned) >= limit:
            break
    return planned


def run_email_identity_dedup(
    store: IdentityEmailDedupStore,
    *,
    dry_run: bool = True,
    limit: int = 0,
    operator_id: str = "reconcile_identity_emails",
) -> dict[str, Any]:
    before = int(store.count_duplicate_email_groups())
    groups = store.find_duplicate_email_groups(limit=limit if limit > 0 else 0)
    planned = plan_email_dedup_groups(groups, limit=limit)
    merged_groups: list[dict[str, Any]] = []
    engagements_repointed = 0
    identities_deleted = 0

    if not dry_run:
        for item in planned:
            result = store.merge_email_duplicate_group(
                email_norm=item["email_norm"],
                canonical_identity_id=item["canonical_identity_id"],
                duplicate_identity_ids=item["duplicate_identity_ids"],
                operator_id=operator_id,
            )
            merged_groups.append(result)
            engagements_repointed += int(result.get("engagements_repointed") or 0)
            identities_deleted += int(result.get("identities_deleted") or 0)

    after = before if dry_run else int(store.count_duplicate_email_groups())
    return {
        "dry_run": dry_run,
        "duplicate_groups_before": before,
        "duplicate_groups_after": after,
        "planned_groups": len(planned),
        "merged_groups": len(merged_groups) if not dry_run else 0,
        "engagements_repointed": engagements_repointed,
        "identities_deleted": identities_deleted,
        "groups": planned if dry_run else merged_groups,
    }
