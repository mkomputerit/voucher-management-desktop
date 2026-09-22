"""Encrypted manual exchange of print-history state between workstations.

The exchange format deliberately carries only audit history and the portable
HMAC identity. It never carries controller settings, PDFs, logos or TLS trust.

A package is always wrapped in the authenticated backup crypto container.
Import is a merge, not a replacement: new generate events use a stable
event_id, modern print events use print_job_id plus voucher identity, and
legacy rows without stable IDs are reconciled as a multiset.
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .backup_crypto import (
    ProtectedBackupAuthenticationError,
    ProtectedBackupError,
    ProtectedBackupWriter,
    decrypt_backup_to_file,
    validate_backup_password,
)
from .history import HistoryError, HistoryService
from .identity import PRODUCT_NAME
from .locking import LockTimeout, exclusive_file_lock


EXCHANGE_FORMAT = 1
EXCHANGE_KIND = "voucher-management-history-exchange"
MANIFEST_NAME = "history_exchange_manifest.json"
HISTORY_NAME = "history.jsonl"
IDENTITY_NAME = "identity.key"
MAX_EXCHANGE_BYTES = 64 * 1024 * 1024
MAX_EVENTS = 500_000
MAX_LINE_BYTES = 1024 * 1024
MAX_IDENTITY_BYTES = 4096


class HistoryExchangeError(RuntimeError):
    """Raised when a history exchange cannot be validated or merged safely."""


@dataclass(frozen=True)
class HistoryImportPlan:
    """Validated import preview retained in memory until operator confirmation."""

    incoming_fingerprint: str
    incoming_events: int
    new_events: int
    duplicate_events: int
    conflict_events: int
    can_adopt_identity: bool
    identity_relation: str
    _rows: tuple[dict, ...] = field(repr=False)
    _secret: str = field(repr=False)


def _canonical_row(row: dict) -> str:
    return json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_row(row: object, line_number: int) -> dict:
    if not isinstance(row, dict):
        raise HistoryExchangeError(
            f"Pacchetto storico non valido alla riga {line_number}"
        )

    event = row.get("event", "generate")
    if event not in {"generate", "print"}:
        raise HistoryExchangeError(
            f"Evento storico non supportato alla riga {line_number}"
        )

    voucher_id = str(row.get("voucher_id", "") or "")
    if (
        len(voucher_id) != 64
        or any(ch not in "0123456789abcdef" for ch in voucher_id.lower())
    ):
        raise HistoryExchangeError(
            f"Identificativo voucher non valido alla riga {line_number}"
        )

    timestamp = str(row.get("timestamp", "") or "").strip()
    if not timestamp:
        raise HistoryExchangeError(
            f"Timestamp storico mancante alla riga {line_number}"
        )

    event_id = str(row.get("event_id", "") or "").strip()
    if event_id and (
        len(event_id) > 128
        or any(
            ch not in "0123456789abcdef-"
            for ch in event_id.lower()
        )
    ):
        raise HistoryExchangeError(
            f"Identificativo evento non valido alla riga {line_number}"
        )

    output_file = str(row.get("output_file", "") or "")
    if output_file:
        if (
            Path(output_file).name != output_file
            or "/" in output_file
            or "\\" in output_file
        ):
            raise HistoryExchangeError(
                f"Nome PDF non portabile alla riga {line_number}"
            )

    if event == "print":
        for key in ("document_copies", "physical_copies"):
            value = row.get(key, 1)
            if type(value) is not int or value < 1:
                raise HistoryExchangeError(
                    f"Contatore stampa non valido alla riga {line_number}"
                )
        job_id = str(row.get("print_job_id", "") or "").strip()
        if job_id and (
            len(job_id) > 128
            or any(
                ch not in "0123456789abcdef-"
                for ch in job_id.lower()
            )
        ):
            raise HistoryExchangeError(
                f"Identificativo stampa non valido alla riga {line_number}"
            )

    return dict(row)


def _parse_history_bytes(payload: bytes) -> list[dict]:
    if len(payload) > MAX_EXCHANGE_BYTES:
        raise HistoryExchangeError("Cronologia esportata troppo grande")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HistoryExchangeError(
            "Cronologia esportata non codificata in UTF-8"
        ) from exc

    rows: list[dict] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise HistoryExchangeError(
                f"Riga storico troppo grande alla riga {line_number}"
            )
        if len(rows) >= MAX_EVENTS:
            raise HistoryExchangeError(
                "Il pacchetto contiene troppi eventi di cronologia"
            )
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HistoryExchangeError(
                f"JSON storico non valido alla riga {line_number}"
            ) from exc
        rows.append(_validate_row(row, line_number))
    return rows


def _modern_event_key(row: dict) -> tuple[str, ...] | None:
    event_id = str(row.get("event_id", "") or "").strip()
    if event_id:
        return ("event", event_id)

    if row.get("event", "generate") != "print":
        return None
    job_id = str(row.get("print_job_id", "") or "").strip()
    if not job_id:
        return None
    return (
        "print",
        job_id,
        str(row.get("voucher_id", "") or ""),
    )


def _merge_delta(
    local_rows: list[dict],
    incoming_rows: list[dict],
) -> tuple[list[dict], int, int]:
    """Return rows to append and count of already-present events.

    Rows without a stable event identity are reconciled as a multiset. This
    preserves compatibility with historical data while preventing new events
    from collapsing during workstation convergence.
    """

    local_modern: dict[tuple[str, ...], str] = {}
    for row in local_rows:
        key = _modern_event_key(row)
        if key is None:
            continue
        canonical = _canonical_row(row)
        previous = local_modern.get(key)
        if previous is not None and previous != canonical:
            raise HistoryExchangeError(
                "La cronologia locale contiene un conflitto di identificativo evento"
            )
        if previous is not None:
            raise HistoryExchangeError(
                "La cronologia locale contiene un identificativo evento duplicato"
            )
        local_modern[key] = canonical

    incoming_modern: dict[tuple[str, ...], str] = {}
    for row in incoming_rows:
        key = _modern_event_key(row)
        if key is None:
            continue
        canonical = _canonical_row(row)
        if key in incoming_modern:
            raise HistoryExchangeError(
                "Il pacchetto contiene un identificativo evento duplicato"
            )
        incoming_modern[key] = canonical

    local_legacy = Counter(
        _canonical_row(row)
        for row in local_rows
        if _modern_event_key(row) is None
    )

    new_rows: list[dict] = []
    duplicates = 0
    conflicts = 0
    for row in incoming_rows:
        modern_key = _modern_event_key(row)
        canonical = _canonical_row(row)
        if modern_key is not None:
            existing = local_modern.get(modern_key)
            if existing is None:
                new_rows.append(row)
            elif existing == canonical:
                duplicates += 1
            else:
                conflicts += 1
            continue

        if local_legacy[canonical] > 0:
            local_legacy[canonical] -= 1
            duplicates += 1
        else:
            new_rows.append(row)

    return new_rows, duplicates, conflicts


class HistoryExchangeService:
    """Export and merge encrypted workstation-local audit history."""

    def __init__(self, history: HistoryService):
        self.history = history

    def _current_rows(self) -> list[dict]:
        try:
            return list(self.history._items() or ())
        except HistoryError as exc:
            raise HistoryExchangeError(str(exc)) from exc

    def export(self, destination: Path, password: str) -> Path:
        password = validate_backup_password(password)
        try:
            self.history.assert_no_pending_print_audit()
        except HistoryError as exc:
            raise HistoryExchangeError(str(exc)) from exc
        try:
            with exclusive_file_lock(self.history.lock_path):
                state = self.history.identity_state()
                if not state.ready:
                    raise HistoryExchangeError(
                        "L'identità della cronologia locale non è verificata"
                    )

                secret = self.history.secret_store.get()
                if not secret:
                    raise HistoryExchangeError(
                        "La chiave portabile della cronologia non è disponibile"
                    )
                fingerprint = self.history.fingerprint(secret)
                if fingerprint != state.local_fingerprint:
                    raise HistoryExchangeError(
                        "Fingerprint della cronologia locale non verificabile"
                    )

                rows = [
                    _validate_row(row, index)
                    for index, row in enumerate(
                        self._current_rows(),
                        start=1,
                    )
                ]
        except LockTimeout as exc:
            raise HistoryExchangeError(
                "Cronologia locale occupata da un'altra operazione"
            ) from exc
        if not rows:
            raise HistoryExchangeError(
                "Non ci sono eventi di cronologia da esportare"
            )

        destination = Path(destination)
        try:
            inside_data_root = destination.resolve().is_relative_to(
                self.history.history_path.parent.parent.resolve()
            )
        except (OSError, RuntimeError):
            inside_data_root = False
        if inside_data_root:
            raise HistoryExchangeError(
                "Salvare l'esportazione fuori dalla cartella dati "
                "dell'applicazione"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(destination.suffix + ".tmp")
        temp.unlink(missing_ok=True)
        manifest = {
            "format": EXCHANGE_FORMAT,
            "kind": EXCHANGE_KIND,
            "application": PRODUCT_NAME,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "history_key_fingerprint": fingerprint,
            "event_count": len(rows),
        }
        history_payload = "".join(
            _canonical_row(row) + "\n"
            for row in rows
        ).encode("utf-8")

        try:
            with ProtectedBackupWriter(temp, password) as protected:
                with zipfile.ZipFile(
                    protected,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                ) as archive:
                    archive.writestr(
                        MANIFEST_NAME,
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            indent=2,
                        ).encode("utf-8"),
                    )
                    archive.writestr(HISTORY_NAME, history_payload)
                    archive.writestr(
                        IDENTITY_NAME,
                        secret.encode("utf-8"),
                    )
            os.replace(temp, destination)
            return destination
        except Exception as exc:
            temp.unlink(missing_ok=True)
            if isinstance(
                exc,
                (HistoryExchangeError, ValueError, ProtectedBackupError),
            ):
                raise HistoryExchangeError(str(exc)) from exc
            raise HistoryExchangeError(
                "Esportazione cronologia non riuscita"
            ) from exc

    @staticmethod
    def _read_package(
        source: Path,
        password: str,
    ) -> tuple[dict, list[dict], str]:
        password = validate_backup_password(password)
        source = Path(source)

        with tempfile.TemporaryFile(mode="w+b") as decrypted:
            try:
                decrypt_backup_to_file(source, decrypted, password)
            except ProtectedBackupAuthenticationError as exc:
                raise HistoryExchangeError(
                    "Password non valida oppure pacchetto storico alterato"
                ) from exc
            except (ProtectedBackupError, ValueError) as exc:
                raise HistoryExchangeError(str(exc)) from exc

            try:
                decrypted.seek(0, os.SEEK_END)
                if decrypted.tell() > MAX_EXCHANGE_BYTES:
                    raise HistoryExchangeError(
                        "Pacchetto storico troppo grande"
                    )
                decrypted.seek(0)
                with zipfile.ZipFile(decrypted, "r") as archive:
                    infos = archive.infolist()
                    names = [info.filename for info in infos]
                    expected = {
                        MANIFEST_NAME,
                        HISTORY_NAME,
                        IDENTITY_NAME,
                    }
                    if set(names) != expected or len(names) != len(expected):
                        raise HistoryExchangeError(
                            "Contenuto del pacchetto storico non riconosciuto"
                        )
                    if any(info.is_dir() for info in infos):
                        raise HistoryExchangeError(
                            "Struttura del pacchetto storico non valida"
                        )

                    by_name = {
                        info.filename: info
                        for info in infos
                    }
                    if by_name[MANIFEST_NAME].file_size > 64 * 1024:
                        raise HistoryExchangeError(
                            "Manifest del pacchetto storico troppo grande"
                        )
                    if by_name[HISTORY_NAME].file_size > MAX_EXCHANGE_BYTES:
                        raise HistoryExchangeError(
                            "Cronologia esportata troppo grande"
                        )
                    if by_name[IDENTITY_NAME].file_size > MAX_IDENTITY_BYTES:
                        raise HistoryExchangeError(
                            "Identità del pacchetto storico non valida"
                        )
                    total_uncompressed = sum(
                        int(info.file_size)
                        for info in infos
                    )
                    if total_uncompressed > (
                        MAX_EXCHANGE_BYTES
                        + MAX_IDENTITY_BYTES
                        + 64 * 1024
                    ):
                        raise HistoryExchangeError(
                            "Pacchetto storico troppo grande"
                        )

                    manifest_raw = archive.read(MANIFEST_NAME)
                    history_raw = archive.read(HISTORY_NAME)
                    identity_raw = archive.read(IDENTITY_NAME)
            except (
                OSError,
                zipfile.BadZipFile,
                KeyError,
            ) as exc:
                raise HistoryExchangeError(
                    "Pacchetto storico danneggiato o non riconosciuto"
                ) from exc

        if len(manifest_raw) > 64 * 1024:
            raise HistoryExchangeError(
                "Manifest del pacchetto storico troppo grande"
            )
        if len(identity_raw) > MAX_IDENTITY_BYTES:
            raise HistoryExchangeError(
                "Identità del pacchetto storico non valida"
            )
        try:
            manifest = json.loads(manifest_raw.decode("utf-8"))
            secret = identity_raw.decode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HistoryExchangeError(
                "Metadati del pacchetto storico non validi"
            ) from exc

        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != EXCHANGE_FORMAT
            or manifest.get("kind") != EXCHANGE_KIND
            or manifest.get("application") != PRODUCT_NAME
        ):
            raise HistoryExchangeError(
                "Formato del pacchetto storico non supportato"
            )

        rows = _parse_history_bytes(history_raw)
        if manifest.get("event_count") != len(rows):
            raise HistoryExchangeError(
                "Conteggio eventi del pacchetto storico non coerente"
            )
        if len(secret.strip()) < 16 or secret != secret.strip():
            raise HistoryExchangeError(
                "Chiave del pacchetto storico non valida"
            )

        fingerprint = HistoryService.fingerprint(secret)
        expected_fp = str(
            manifest.get("history_key_fingerprint", "") or ""
        ).strip().lower()
        if fingerprint != expected_fp:
            raise HistoryExchangeError(
                "Fingerprint del pacchetto storico non verificabile"
            )

        return manifest, rows, secret

    def prepare_import(
        self,
        source: Path,
        password: str,
    ) -> HistoryImportPlan:
        try:
            self.history.assert_no_pending_print_audit()
        except HistoryError as exc:
            raise HistoryExchangeError(str(exc)) from exc
        manifest, rows, secret = self._read_package(source, password)
        incoming_fp = str(
            manifest["history_key_fingerprint"]
        ).lower()

        try:
            with exclusive_file_lock(self.history.lock_path):
                local_rows = self._current_rows()
                state = self.history.identity_state()
                local_secret = self.history.secret_store.get()
                local_fp = (
                    self.history.fingerprint(local_secret)
                    if local_secret
                    else ""
                )

                if local_rows:
                    if not state.ready:
                        raise HistoryExchangeError(
                            "La cronologia locale ha un'identità non verificata; "
                            "risolverla prima dell'import"
                        )
                    if local_fp != incoming_fp:
                        raise HistoryExchangeError(
                            "Il pacchetto appartiene a una diversa identità "
                            "di cronologia"
                        )
                    relation = "same_identity"
                    can_adopt = False
                else:
                    if state.ready and local_fp == incoming_fp:
                        relation = "same_identity"
                        can_adopt = False
                    else:
                        relation = "empty_local_identity"
                        can_adopt = True

                new_rows, duplicates, conflicts = _merge_delta(
                    local_rows,
                    rows,
                )
        except LockTimeout as exc:
            raise HistoryExchangeError(
                "Cronologia locale occupata da un'altra operazione"
            ) from exc

        return HistoryImportPlan(
            incoming_fingerprint=incoming_fp,
            incoming_events=len(rows),
            new_events=len(new_rows),
            duplicate_events=duplicates,
            conflict_events=conflicts,
            can_adopt_identity=can_adopt,
            identity_relation=relation,
            _rows=tuple(rows),
            _secret=secret,
        )

    def apply_import(
        self,
        plan: HistoryImportPlan,
        *,
        adopt_identity: bool = False,
    ) -> int:
        """Atomically merge a previously validated import plan."""

        try:
            self.history.assert_no_pending_print_audit()
        except HistoryError as exc:
            raise HistoryExchangeError(str(exc)) from exc

        try:
            with exclusive_file_lock(self.history.lock_path):
                local_rows = self._current_rows()
                state = self.history.identity_state()
                local_secret = self.history.secret_store.get()
                local_fp = (
                    self.history.fingerprint(local_secret)
                    if local_secret
                    else ""
                )

                identity_change = False
                if local_rows:
                    if not state.ready or local_fp != plan.incoming_fingerprint:
                        raise HistoryExchangeError(
                            "L'identità locale è cambiata dopo l'anteprima "
                            "dell'import"
                        )
                elif local_fp != plan.incoming_fingerprint or not state.ready:
                    if not adopt_identity:
                        raise HistoryExchangeError(
                            "L'import richiede l'adozione esplicita "
                            "dell'identità del pacchetto"
                        )
                    identity_change = True

                new_rows, _duplicates, conflicts = _merge_delta(
                    local_rows,
                    list(plan._rows),
                )
                if conflicts:
                    raise HistoryExchangeError(
                        "Il pacchetto contiene conflitti con stampe già "
                        "registrate; il merge è stato annullato"
                    )

                original_settings = self.history.settings_store.load()
                original_secret = local_secret
                original_history = (
                    self.history.history_path.read_bytes()
                    if self.history.history_path.exists()
                    else None
                )

                try:
                    if identity_change:
                        self.history.secret_store.set(plan._secret)
                        updated = dict(original_settings)
                        updated["history_key_fingerprint"] = (
                            plan.incoming_fingerprint
                        )
                        self.history.settings_store.save(updated)

                    if new_rows:
                        self._replace_history(
                            [*local_rows, *new_rows]
                        )

                    verified_rows = self._current_rows()
                    _remaining, duplicates, conflicts = _merge_delta(
                        verified_rows,
                        list(plan._rows),
                    )
                    if (
                        _remaining
                        or conflicts
                        or duplicates != len(plan._rows)
                    ):
                        raise HistoryExchangeError(
                            "Verifica del merge storico non riuscita"
                        )
                except Exception:
                    self._restore_transaction(
                        original_history,
                        original_settings,
                        original_secret,
                    )
                    raise

                return len(new_rows)
        except LockTimeout as exc:
            raise HistoryExchangeError(
                "Cronologia locale occupata da un'altra operazione"
            ) from exc
        except HistoryExchangeError:
            raise
        except Exception as exc:
            raise HistoryExchangeError(
                "Importazione cronologia non riuscita"
            ) from exc

    def _replace_history(self, rows: list[dict]) -> None:
        self.history.history_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        temp = self.history.history_path.with_suffix(
            self.history.history_path.suffix + ".import.tmp"
        )
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                for row in rows:
                    handle.write(_canonical_row(row) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.history.history_path)
        finally:
            temp.unlink(missing_ok=True)

    def _restore_transaction(
        self,
        history_bytes: bytes | None,
        settings: dict,
        secret: str | None,
    ) -> None:
        if history_bytes is None:
            self.history.history_path.unlink(missing_ok=True)
        else:
            temp = self.history.history_path.with_suffix(
                self.history.history_path.suffix + ".rollback.tmp"
            )
            try:
                temp.write_bytes(history_bytes)
                os.replace(temp, self.history.history_path)
            finally:
                temp.unlink(missing_ok=True)

        self.history.settings_store.save(settings)
        if secret:
            self.history.secret_store.set(secret)
        else:
            self.history.secret_store.clear()
