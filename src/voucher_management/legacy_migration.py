"""Fail-closed planning for migration of legacy 4.x HMAC audit history.

This module does not mutate SQLite or legacy files.  It proves which historical
rows can be associated with independently known voucher codes before the
transactional apply phase is allowed to exist.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class LegacyMigrationError(RuntimeError):
    """Raised when legacy evidence cannot be interpreted safely."""


@dataclass(frozen=True)
class LegacyVoucherCandidate:
    """One independently known voucher that may explain a legacy HMAC."""

    controller_id: int
    unifi_id: str
    code: str

    @property
    def key(self) -> tuple[int, str]:
        return (self.controller_id, self.unifi_id)


@dataclass(frozen=True)
class LegacyAuditRow:
    """Validated legacy row plus its original line identity."""

    line_number: int
    event: str
    voucher_digest: str
    timestamp: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class ResolvedLegacyRow:
    """A row whose HMAC maps to exactly one independently known voucher."""

    row: LegacyAuditRow
    candidate: LegacyVoucherCandidate


@dataclass(frozen=True)
class AmbiguousLegacyRow:
    """A row whose HMAC maps to more than one possible voucher record."""

    row: LegacyAuditRow
    candidates: tuple[LegacyVoucherCandidate, ...]


@dataclass(frozen=True)
class UnresolvedLegacyRow:
    """A valid historical row for which no independently known code matches."""

    row: LegacyAuditRow
    reason: str = "no_known_code"


@dataclass(frozen=True)
class LegacyMigrationPlan:
    """Immutable dry-run result used as the boundary before migration writes."""

    history_fingerprint: str
    resolved: tuple[ResolvedLegacyRow, ...]
    ambiguous: tuple[AmbiguousLegacyRow, ...]
    unresolved: tuple[UnresolvedLegacyRow, ...]

    @property
    def total_rows(self) -> int:
        return len(self.resolved) + len(self.ambiguous) + len(self.unresolved)

    @property
    def fully_resolved(self) -> bool:
        return not self.ambiguous and not self.unresolved


_HEX = frozenset("0123456789abcdef")


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _digest(code: str, secret: bytes) -> str:
    try:
        encoded = code.encode("ascii")
    except UnicodeEncodeError as exc:
        raise LegacyMigrationError(
            "Codice voucher candidato non ASCII"
        ) from exc
    return hmac.new(secret, encoded, hashlib.sha256).hexdigest()


def _candidate_spellings(code: str) -> tuple[str, ...]:
    """Return deterministic spellings used by the 4.x print-history path."""

    value = str(code).strip()
    if not value:
        raise LegacyMigrationError("Codice voucher candidato vuoto")
    compact = value.replace("-", "")
    spellings = [value]
    if len(compact) == 10:
        formatted = f"{compact[:5]}-{compact[5:]}"
        if formatted not in spellings:
            spellings.append(formatted)
    return tuple(spellings)


def _validated_history_rows(history_path: Path) -> tuple[LegacyAuditRow, ...]:
    """Parse the complete legacy file or fail on the first unsafe row."""

    history_path = Path(history_path)
    if not history_path.exists():
        return ()
    if not history_path.is_file():
        raise LegacyMigrationError("Cronologia legacy non leggibile")

    rows: list[LegacyAuditRow] = []
    try:
        with history_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LegacyMigrationError(
                        f"Cronologia legacy danneggiata alla riga {line_number}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise LegacyMigrationError(
                        f"Record legacy non valido alla riga {line_number}"
                    )

                event = str(payload.get("event", "generate") or "generate")
                if event not in {"generate", "print"}:
                    raise LegacyMigrationError(
                        f"Evento legacy non supportato alla riga {line_number}"
                    )

                digest = str(payload.get("voucher_id", "") or "").lower()
                if len(digest) != 64 or any(ch not in _HEX for ch in digest):
                    raise LegacyMigrationError(
                        f"HMAC voucher non valido alla riga {line_number}"
                    )

                timestamp = str(payload.get("timestamp", "") or "").strip()
                if not timestamp:
                    raise LegacyMigrationError(
                        f"Timestamp legacy mancante alla riga {line_number}"
                    )

                if event == "print":
                    for field in ("document_copies", "physical_copies"):
                        value = payload.get(field, 1)
                        if type(value) is not int or value < 1:
                            raise LegacyMigrationError(
                                f"Conteggio stampa legacy non valido alla riga "
                                f"{line_number}"
                            )

                rows.append(
                    LegacyAuditRow(
                        line_number=line_number,
                        event=event,
                        voucher_digest=digest,
                        timestamp=timestamp,
                        payload=dict(payload),
                    )
                )
    except LegacyMigrationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise LegacyMigrationError(
            "Cronologia legacy non leggibile"
        ) from exc

    return tuple(rows)


def build_legacy_migration_plan(
    *,
    history_path: Path,
    expected_fingerprint: str,
    secret: str,
    candidates: Sequence[LegacyVoucherCandidate],
) -> LegacyMigrationPlan:
    """Build a migration plan without modifying either legacy or SQLite state.

    The history key must match the fingerprint stored with the 4.x settings.
    Each HMAC is then recomputed only from independently known candidate codes.
    Zero matches preserve the row as unresolved evidence; multiple distinct
    voucher identities preserve it as ambiguous evidence.
    """

    expected = str(expected_fingerprint or "").strip().lower()
    secret = str(secret or "")
    if (
        len(expected) != 16
        or any(ch not in _HEX for ch in expected)
        or not secret
        or _fingerprint(secret) != expected
    ):
        raise LegacyMigrationError(
            "Identità HMAC della cronologia legacy non verificabile"
        )

    by_digest: dict[str, dict[tuple[int, str], LegacyVoucherCandidate]] = {}
    secret_bytes = secret.encode("utf-8")

    for candidate in candidates:
        if candidate.controller_id < 1:
            raise LegacyMigrationError("Controller candidato non valido")
        if not str(candidate.unifi_id).strip():
            raise LegacyMigrationError("Identificativo UniFi candidato vuoto")
        for spelling in _candidate_spellings(candidate.code):
            digest = _digest(spelling, secret_bytes)
            by_digest.setdefault(digest, {})[candidate.key] = candidate

    resolved: list[ResolvedLegacyRow] = []
    ambiguous: list[AmbiguousLegacyRow] = []
    unresolved: list[UnresolvedLegacyRow] = []

    for row in _validated_history_rows(history_path):
        matches = tuple(
            sorted(
                by_digest.get(row.voucher_digest, {}).values(),
                key=lambda item: (item.controller_id, item.unifi_id),
            )
        )
        if len(matches) == 1:
            resolved.append(
                ResolvedLegacyRow(row=row, candidate=matches[0])
            )
        elif len(matches) > 1:
            ambiguous.append(
                AmbiguousLegacyRow(row=row, candidates=matches)
            )
        else:
            unresolved.append(UnresolvedLegacyRow(row=row))

    return LegacyMigrationPlan(
        history_fingerprint=expected,
        resolved=tuple(resolved),
        ambiguous=tuple(ambiguous),
        unresolved=tuple(unresolved),
    )
