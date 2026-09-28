"""Operator-facing recent activity for the 5.1 workspace.

The dashboard reads only durable SQLite facts. It never invents exact usage
timestamps from UniFi counters and never needs controller credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .database import Database


@dataclass(frozen=True)
class WorkspaceActivity:
    """One concise activity row for the Home dashboard."""

    occurred_at: str
    title: str
    detail: str


def _sort_time(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.replace(tzinfo=None) - parsed.utcoffset()
        return parsed
    except (TypeError, ValueError, OverflowError):
        return datetime.min


def load_recent_workspace_activity(
    database: Database,
    *,
    controller_id: int | None,
    limit: int = 7,
) -> tuple[WorkspaceActivity, ...]:
    """Return recent sync, print and voucher-discovery facts for one workspace."""

    if limit < 1:
        return ()

    activities: list[WorkspaceActivity] = []
    controller_clause = ""
    params: tuple[object, ...] = ()
    if controller_id is not None:
        controller_clause = " AND v.controller_id=?"
        params = (int(controller_id),)

    for row in database.connection.execute(
        f"""SELECT vp.printed_at, vp.is_reprint, v.name, v.code
            FROM voucher_prints AS vp
            JOIN vouchers AS v ON v.id=vp.voucher_id
            WHERE 1=1 {controller_clause}
            ORDER BY vp.printed_at DESC
            LIMIT ?""",
        (*params, limit),
    ):
        label = str(row["name"] or "").strip() or str(row["code"] or "").strip()
        activities.append(
            WorkspaceActivity(
                occurred_at=str(row["printed_at"] or ""),
                title=(
                    "Voucher ristampato"
                    if bool(row["is_reprint"])
                    else "Voucher stampato"
                ),
                detail=label or "Voucher",
            )
        )

    sync_clause = ""
    sync_params: tuple[object, ...] = ()
    if controller_id is not None:
        sync_clause = " AND controller_id=?"
        sync_params = (int(controller_id),)
    for row in database.connection.execute(
        f"""SELECT completed_at, vouchers_received, changes_detected
            FROM sync_runs
            WHERE status='SUCCESS'
              AND completed_at IS NOT NULL
              {sync_clause}
            ORDER BY completed_at DESC
            LIMIT ?""",
        (*sync_params, limit),
    ):
        received = int(row["vouchers_received"] or 0)
        changes = int(row["changes_detected"] or 0)
        activities.append(
            WorkspaceActivity(
                occurred_at=str(row["completed_at"] or ""),
                title="Sincronizzazione completata",
                detail=f"{received} voucher • {changes} variazioni rilevate",
            )
        )

    for row in database.connection.execute(
        f"""SELECT v.imported_at, v.name, v.code
            FROM vouchers AS v
            JOIN controllers AS c ON c.id=v.controller_id
            WHERE v.archived_at IS NULL
              AND c.api_root NOT LIKE 'legacy-backup://%'
              {controller_clause}
            ORDER BY v.imported_at DESC
            LIMIT ?""",
        (*params, limit),
    ):
        label = str(row["name"] or "").strip() or str(row["code"] or "").strip()
        activities.append(
            WorkspaceActivity(
                occurred_at=str(row["imported_at"] or ""),
                title="Voucher rilevato",
                detail=label or "Voucher",
            )
        )

    activities.sort(
        key=lambda item: _sort_time(item.occurred_at),
        reverse=True,
    )
    return tuple(activities[:limit])
