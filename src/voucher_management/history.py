"""Privacy-preserving local PDF/print audit trail.

The history is safety-relevant: first physical print is the point after which
Voucher Management refuses application-side deletion. Any ambiguity in history
or key identity therefore fails closed instead of being interpreted as empty.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .locking import LockTimeout, exclusive_file_lock
from .models import VoucherBatch
from .security.history_key import HistoryKeyStore
from .settings import SettingsStore


class HistoryError(RuntimeError):
    """Raised when the persistent PDF/print audit trail is unavailable or invalid."""


@dataclass
class DuplicateHit:
    digest: str
    recipient: str
    timestamp: str
    output_file: str


@dataclass
class PrintStats:
    """User-facing audit data for one voucher without storing its code in clear."""
    generated_documents: int = 0
    generated_copies: int = 0
    print_jobs: int = 0
    printed_copies: int = 0
    latest_output_file: str = ""
    # The first physical print is a lifecycle boundary in the operator UI:
    # once a code has been printed it can no longer be deleted by this tool.
    first_print_utc: str = ""


@dataclass(frozen=True)
class HistoryIdentityState:
    """Describe whether the local audit identity can be used or recovered."""

    ready: bool
    reason: str
    expected_fingerprint: str = ""
    local_fingerprint: str = ""
    can_adopt_local_key: bool = False


@dataclass(frozen=True)
class PendingPrintAudit:
    """Durable state for one physical print that is not fully audited yet."""

    state: str
    audit_id: str
    records: tuple[dict, ...]


@dataclass(frozen=True)
class ResolvedPendingPrint:
    """Pending print reconstructed from HMAC records and known voucher codes."""

    state: str
    audit_id: str
    codes: tuple[str, ...]
    output_file: str
    document_copies: int
    submitted_at: str


class HistoryService:
    """Persist PDF/print audit data using privacy-preserving HMAC identifiers.

    The audit trail stores HMAC identifiers rather than voucher codes in clear.
    Key storage is injected so the application can use its portable persistent
    key while tests can use isolated stores. Missing/mismatched material is
    repaired for future events without rewriting historical rows.
    """

    def __init__(self, history_path: Path, lock_path: Path, settings_store: SettingsStore,
                 secret_store: HistoryKeyStore | None = None):
        self.history_path = history_path
        self.lock_path = lock_path
        self.settings_store = settings_store
        self.secret_store = secret_store or HistoryKeyStore()
        self.pending_print_path = (
            self.history_path.parent / "pending_print_audit.json"
        )
        self.recovered_key = False
        self.ensure_ready()

    @staticmethod
    def fingerprint(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]

    def _new_secret(self) -> str:
        secret = secrets.token_hex(32)
        self.secret_store.set(secret)
        return secret

    def _history_has_rows(self) -> bool:
        """Return whether persistent audit data already exists.

        Any non-empty history file is treated as evidence that an existing HMAC
        identity may be required. We must not rotate the key behind that data:
        doing so would make previously printed vouchers look unprinted.
        """
        try:
            return self.history_path.is_file() and self.history_path.stat().st_size > 0
        except OSError:
            # An unreadable history is not a safe reason to create a new key.
            return True

    def ensure_ready(self) -> bool:
        """Initialize a history key without invalidating existing audit rows.

        A missing/mismatched key may be replaced only when there is no existing
        audit history. If rows already exist, keep the mismatch visible so all
        lifecycle-sensitive operations fail closed until the correct backup/key
        is restored.
        """
        settings = self.settings_store.load()
        expected = settings.get("history_key_fingerprint", "")
        secret = self.secret_store.get()

        if secret:
            actual = self.fingerprint(secret)
            if expected and actual == expected:
                return True
            if not expected and not self._history_has_rows():
                # With no history there is nothing to correlate, so a valid
                # existing portable key can safely become the initial identity.
                settings["history_key_fingerprint"] = actual
                self.settings_store.save(settings)
                return True

        if self._history_has_rows():
            # Existing rows plus missing/mismatched identity are ambiguous.
            # Never rotate or bless a key behind those rows.
            return False

        secret = self._new_secret()
        settings["history_key_fingerprint"] = self.fingerprint(secret)
        self.settings_store.save(settings)
        self.recovered_key = bool(expected)
        return True

    def identity_state(self) -> HistoryIdentityState:
        """Return explicit recovery state without changing persistent identity."""

        current = self.settings_store.load()
        expected = str(
            current.get("history_key_fingerprint", "") or ""
        ).strip().lower()
        secret = self.secret_store.get()
        local = self.fingerprint(secret) if secret else ""
        has_rows = self._history_has_rows()

        if expected and local and local == expected:
            return HistoryIdentityState(
                ready=True,
                reason="ready",
                expected_fingerprint=expected,
                local_fingerprint=local,
            )
        if not secret:
            return HistoryIdentityState(
                ready=False,
                reason="missing_local_key",
                expected_fingerprint=expected,
            )
        if not expected:
            return HistoryIdentityState(
                ready=False,
                reason="missing_fingerprint",
                local_fingerprint=local,
                can_adopt_local_key=has_rows,
            )
        return HistoryIdentityState(
            ready=False,
            reason="key_mismatch",
            expected_fingerprint=expected,
            local_fingerprint=local,
        )

    def status(self) -> tuple[bool, str]:
        state = self.identity_state()
        if state.ready:
            return True, "Cronologia stampe: disponibile"
        if state.reason == "missing_fingerprint":
            return False, "Cronologia stampe: fingerprint mancante"
        if state.reason == "missing_local_key":
            return False, "Cronologia stampe: chiave locale mancante"
        return False, "Cronologia stampe: chiave locale non corrispondente"

    def adopt_present_secret(self, confirmation: str) -> str:
        """Adopt the present portable key only when no fingerprint is recorded.

        This recovery path never accepts arbitrary key material and never
        overrides a known, different fingerprint. The operator must type the
        exact displayed fingerprint so adoption cannot happen accidentally.
        """

        state = self.identity_state()
        if state.ready:
            return state.local_fingerprint
        if state.reason == "missing_local_key":
            raise ValueError(
                "La chiave locale della cronologia non è presente. "
                "Ripristinare un backup completo che contenga la chiave."
            )
        if state.reason == "key_mismatch":
            raise ValueError(
                "La chiave locale non corrisponde al fingerprint già "
                "associato alla cronologia. Ripristinare la chiave corretta "
                "da un backup invece di sostituire l'identità."
            )
        if not state.can_adopt_local_key or not state.local_fingerprint:
            raise ValueError(
                "Non esiste una cronologia da associare alla chiave locale."
            )

        typed = (
            str(confirmation)
            .replace(":", "")
            .replace(" ", "")
            .strip()
            .lower()
        )
        if typed != state.local_fingerprint:
            raise ValueError(
                "La conferma non corrisponde al fingerprint della chiave "
                "presente."
            )

        current = self.settings_store.load()
        if current.get("history_key_fingerprint"):
            raise ValueError(
                "Il fingerprint della cronologia è già stato configurato."
            )
        current["history_key_fingerprint"] = state.local_fingerprint
        self.settings_store.save(current)

        verified = self.identity_state()
        if not verified.ready:
            raise HistoryError(
                "Impossibile verificare l'identità della cronologia dopo "
                "l'adozione"
            )
        return verified.local_fingerprint

    def configure_secret(self, secret: str, settings: dict, *, replace: bool = False) -> None:
        """Configure an explicit audit key without replacing a valid key by default."""
        secret = secret.strip()
        if len(secret) < 16:
            raise ValueError("La chiave cronologia deve contenere almeno 16 caratteri")
        fp = self.fingerprint(secret)
        existing = settings.get("history_key_fingerprint", "")
        if existing and existing != fp:
            if self._history_has_rows():
                raise ValueError(
                    "La chiave non può essere sostituita con una cronologia "
                    "già esistente"
                )
            if not replace:
                raise ValueError(
                    "La chiave non corrisponde a quella già associata "
                    "alla cronologia"
                )
        self.secret_store.set(secret)
        if not existing or existing != fp:
            settings["history_key_fingerprint"] = fp
            self.settings_store.save(settings)

    def _secret(self, settings: dict | None = None) -> bytes:
        """Return a usable audit key or raise instead of failing silently."""
        current = self.settings_store.load()
        expected = current.get("history_key_fingerprint", "")
        secret = self.secret_store.get()
        if not expected or not secret or self.fingerprint(secret) != expected:
            self.ensure_ready()
            current = self.settings_store.load()
            expected = current.get("history_key_fingerprint", "")
            secret = self.secret_store.get()
        if not expected or not secret or self.fingerprint(secret) != expected:
            raise HistoryError("Impossibile inizializzare la cronologia locale delle stampe")
        return secret.encode("utf-8")

    @staticmethod
    def _digest(code: str, secret: bytes) -> str:
        return hmac.new(secret, code.encode("ascii"), hashlib.sha256).hexdigest()

    def _items(self):
        """Yield audit rows, failing closed if any persistent row is corrupt.

        Print state is a safety boundary: silently skipping a damaged line could
        hide the only physical-print event for a voucher and incorrectly make
        deletion/reprinting appear safe. A malformed or non-object row therefore
        makes the whole audit trail unavailable until it is repaired/restored.
        """
        if not self.history_path.exists():
            return
        try:
            with self.history_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise HistoryError(
                            "Cronologia danneggiata alla riga "
                            f"{line_number}: JSON non valido"
                        ) from exc
                    if not isinstance(item, dict):
                        raise HistoryError(
                            "Cronologia danneggiata alla riga "
                            f"{line_number}: record non valido"
                        )
                    yield item
        except HistoryError:
            raise
        except (OSError, UnicodeError) as exc:
            raise HistoryError(
                f"Impossibile leggere la cronologia: {self.history_path}"
            ) from exc

    def find_duplicates(self, codes: list[str], settings: dict) -> dict[str, DuplicateHit]:
        secret = self._secret(settings)
        targets = {self._digest(code, secret): code for code in codes}
        hits = {}
        for item in self._items() or ():
            if item.get("event", "generate") != "generate":
                continue
            digest = item.get("voucher_id", "")
            if digest in targets:
                code = targets[digest]
                hits[code] = DuplicateHit(digest=digest, recipient=item.get("recipient", ""), timestamp=item.get("timestamp", ""), output_file=item.get("output_file", ""))
        return hits

    def stats_for_codes(self, codes: list[str], settings: dict) -> dict[str, PrintStats]:
        """Summarise PDF generation, first print and physical print counters."""
        secret = self._secret(settings)
        targets = {self._digest(code, secret): code for code in codes}
        result = {code: PrintStats() for code in codes}
        docs = {code: set() for code in codes}
        for item in self._items() or ():
            code = targets.get(item.get("voucher_id", ""))
            if not code:
                continue
            stat = result[code]
            event = item.get("event", "generate")
            if event == "print":
                stat.print_jobs += 1
                stat.printed_copies += int(item.get("physical_copies", 1) or 1)
                timestamp = str(item.get("timestamp", "") or "")
                if timestamp and (not stat.first_print_utc or timestamp < stat.first_print_utc):
                    stat.first_print_utc = timestamp
            else:
                stat.generated_copies += 1
                output = item.get("output_file", "")
                if output:
                    docs[code].add(output)
                    stat.latest_output_file = output
        for code, stat in result.items():
            stat.generated_documents = len(docs[code])
        return result

    def generated_output_names(self) -> set[str]:
        """Return generated PDF filenames only when audit identity is valid."""

        self._secret()
        names: set[str] = set()
        for item in self._items() or ():
            if item.get("event", "generate") != "generate":
                continue
            value = str(item.get("output_file", "") or "").strip()
            if value:
                names.add(Path(value).name)
        return names

    def codes_for_output(
        self,
        candidate_codes: list[str],
        output_path: Path,
        settings: dict,
    ) -> list[str]:
        """Return candidate voucher codes linked to one generated PDF.

        History stores only HMAC identifiers, so it cannot reverse a digest back
        into a code. The application supplies the voucher codes currently known
        from the controller; this method correlates their digests with every
        generate row for the PDF. Multiplicity is preserved so an unlimited
        voucher printed several times on one sheet records the correct number
        of physical labels when that archived PDF is reprinted.
        """
        secret = self._secret(settings)
        output_name = Path(output_path).name
        digest_to_code = {
            self._digest(code, secret): code
            for code in candidate_codes
        }
        linked: list[str] = []
        for item in self._items() or ():
            if item.get("event", "generate") != "generate":
                continue
            if Path(str(item.get("output_file", "") or "")).name != output_name:
                continue
            code = digest_to_code.get(str(item.get("voucher_id", "") or ""))
            if code:
                linked.append(code)
        return linked

    def record_batch(self, batch: VoucherBatch, output_path: Path, settings: dict, reprint: bool) -> None:
        secret = self._secret(settings)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        output_name = Path(output_path).name
        # Store only the archive filename. Absolute profile paths add no audit
        # value and reduce both portability and privacy of backups.
        records = [
            {
                "event": "generate",
                "event_id": secrets.token_hex(16),
                "voucher_id": self._digest(v.code, secret),
                "recipient": v.recipient or batch.recipient,
                "duration_minutes": v.duration_minutes,
                "timestamp": now,
                "source": "CONTROLLER_API",
                "output_file": output_name,
                "reprint": bool(reprint),
            }
            for v in batch.vouchers
        ]
        self._append(records)
        self._verify_event(records, "generate")

    @staticmethod
    def _canonical_print_records(records: list[dict]) -> str:
        return json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _load_pending_print(self) -> PendingPrintAudit | None:
        path = self.pending_print_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HistoryError(
                "Registrazione stampa pendente danneggiata"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("format") not in {1, 2}
            or not isinstance(payload.get("records"), list)
        ):
            raise HistoryError(
                "Registrazione stampa pendente non valida"
            )

        # Format 1 was written only after Windows submission, so it is
        # equivalent to the explicit "submitted" state introduced by format 2.
        state = (
            "submitted"
            if payload.get("format") == 1
            else str(payload.get("state", "") or "").strip()
        )
        if state not in {"prepared", "submitted"}:
            raise HistoryError(
                "Stato della registrazione stampa pendente non valido"
            )

        audit_id = str(payload.get("audit_id", "") or "").strip()
        records = payload["records"]
        if (
            not audit_id
            or len(audit_id) > 128
            or any(
                ch not in "0123456789abcdef-"
                for ch in audit_id.lower()
            )
            or not records
        ):
            raise HistoryError(
                "Registrazione stampa pendente incompleta"
            )
        normalized: list[dict] = []
        for item in records:
            if (
                not isinstance(item, dict)
                or item.get("event") != "print"
                or item.get("print_job_id") != audit_id
            ):
                raise HistoryError(
                    "Registrazione stampa pendente incoerente"
                )
            voucher_id = str(item.get("voucher_id", "") or "")
            if (
                len(voucher_id) != 64
                or any(
                    ch not in "0123456789abcdef"
                    for ch in voucher_id.lower()
                )
            ):
                raise HistoryError(
                    "Registrazione stampa pendente con voucher non valido"
                )
            timestamp = str(item.get("timestamp", "") or "").strip()
            output_file = str(item.get("output_file", "") or "")
            document_copies = item.get("document_copies")
            physical_copies = item.get("physical_copies")
            if (
                not timestamp
                or not output_file
                or Path(output_file).name != output_file
                or type(document_copies) is not int
                or document_copies < 1
                or type(physical_copies) is not int
                or physical_copies < 1
            ):
                raise HistoryError(
                    "Registrazione stampa pendente con metadati non validi"
                )
            normalized.append(dict(item))
        return PendingPrintAudit(
            state=state,
            audit_id=audit_id,
            records=tuple(normalized),
        )

    def pending_print_state(self) -> str:
        """Return "", "prepared" or "submitted" for local recovery state."""

        pending = self._load_pending_print()
        return pending.state if pending is not None else ""

    def resolve_pending_print(
        self,
        candidate_codes: list[str],
        settings: dict,
    ) -> ResolvedPendingPrint | None:
        """Resolve HMAC-only pending rows against independently known codes.

        The pending descriptor deliberately contains no voucher plaintext.
        During 5.0 recovery the SQLite snapshot supplies candidate codes; only
        exact HMAC matches are accepted, so recovery never guesses identity.
        """

        pending = self._load_pending_print()
        if pending is None:
            return None

        secret = self._secret(settings)
        digest_to_code: dict[str, str] = {}
        for code in candidate_codes:
            normalized = str(code).strip()
            if not normalized:
                continue
            digest = self._digest(normalized, secret)
            previous = digest_to_code.get(digest)
            if previous is not None and previous != normalized:
                raise HistoryError(
                    "Collisione nella risoluzione della stampa pendente"
                )
            digest_to_code[digest] = normalized

        first = pending.records[0]
        output_file = str(first["output_file"])
        document_copies = int(first["document_copies"])
        submitted_at = str(first["timestamp"])
        resolved_codes: list[str] = []

        for record in pending.records:
            if (
                str(record["output_file"]) != output_file
                or int(record["document_copies"]) != document_copies
                or str(record["timestamp"]) != submitted_at
            ):
                raise HistoryError(
                    "Registrazione stampa pendente con metadati incoerenti"
                )
            code = digest_to_code.get(str(record["voucher_id"]))
            if code is None:
                raise HistoryError(
                    "Impossibile associare un voucher della stampa pendente "
                    "all'archivio locale"
                )
            physical_copies = int(record["physical_copies"])
            if physical_copies % document_copies:
                raise HistoryError(
                    "Registrazione stampa pendente con conteggio copie incoerente"
                )
            resolved_codes.extend(
                [code] * (physical_copies // document_copies)
            )

        return ResolvedPendingPrint(
            state=pending.state,
            audit_id=pending.audit_id,
            codes=tuple(resolved_codes),
            output_file=output_file,
            document_copies=document_copies,
            submitted_at=submitted_at,
        )

    def finalize_pending_print_audit(self, audit_id: str) -> None:
        """Remove the durable marker only after every required audit is safe."""

        self._clear_pending_print(str(audit_id).strip())

    def has_pending_print_audit(self) -> bool:
        """Return whether an unresolved physical-print audit exists."""

        return self._load_pending_print() is not None

    def assert_no_pending_print_audit(self) -> None:
        """Block a new physical print until an earlier audit is resolved."""

        if self.has_pending_print_audit():
            raise HistoryError(
                "Esiste una stampa precedente ancora da risolvere nello "
                "storico. Usare RECUPERA STAMPA PENDENTE prima di inviare "
                "un nuovo documento."
            )

    def _write_pending_print(
        self,
        records: list[dict],
        audit_id: str,
        state: str,
    ) -> None:
        if state not in {"prepared", "submitted"}:
            raise ValueError("Stato stampa pendente non valido")

        self.pending_print_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        temp = self.pending_print_path.with_suffix(
            self.pending_print_path.suffix + ".tmp"
        )
        payload = {
            "format": 2,
            "state": state,
            "audit_id": audit_id,
            "records": records,
        }
        try:
            with temp.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.pending_print_path)
        except OSError as exc:
            raise HistoryError(
                "Impossibile salvare la registrazione stampa pendente"
            ) from exc
        finally:
            temp.unlink(missing_ok=True)

    def _persist_pending_print(
        self,
        records: list[dict],
        audit_id: str,
        *,
        state: str = "submitted",
    ) -> None:
        current = self._load_pending_print()
        if current is not None:
            same_job = (
                current.audit_id == audit_id
                and self._canonical_print_records(list(current.records))
                == self._canonical_print_records(records)
            )
            if not same_job:
                raise HistoryError(
                    "Esiste già una diversa registrazione stampa pendente"
                )
            # Never downgrade a job already known to have been submitted.
            if current.state == "submitted" or current.state == state:
                return
        self._write_pending_print(records, audit_id, state)

    def _clear_pending_print(self, audit_id: str) -> None:
        current = self._load_pending_print()
        if current is None:
            return
        if current.audit_id != audit_id:
            raise HistoryError(
                "La registrazione stampa pendente è cambiata durante il recupero"
            )
        try:
            self.pending_print_path.unlink(missing_ok=True)
        except OSError as exc:
            raise HistoryError(
                "Stampa registrata, ma impossibile cancellare lo stato pendente"
            ) from exc

    def _build_print_records(
        self,
        codes: list[str],
        output_path: Path,
        document_copies: int,
        settings: dict,
        *,
        audit_id: str,
        submitted_at: str,
    ) -> list[dict]:
        secret = self._secret(settings)
        if document_copies < 1:
            raise ValueError(
                "Il numero di copie documento deve essere positivo"
            )
        if (
            not audit_id
            or len(audit_id) > 128
            or any(
                ch not in "0123456789abcdef-"
                for ch in audit_id.lower()
            )
        ):
            raise ValueError("Identificativo audit stampa non valido")
        timestamp = str(submitted_at or "").strip()
        if not timestamp:
            raise ValueError("Timestamp audit stampa non valido")

        counts = Counter(codes)
        output_name = Path(output_path).name
        return [
            {
                "event": "print",
                "voucher_id": self._digest(code, secret),
                "timestamp": timestamp,
                "output_file": output_name,
                "document_copies": document_copies,
                "physical_copies": labels * document_copies,
                "print_job_id": audit_id,
            }
            for code, labels in counts.items()
        ]

    def prepare_print_audit(
        self,
        codes: list[str],
        output_path: Path,
        document_copies: int,
        settings: dict,
        *,
        audit_id: str,
        submitted_at: str,
    ) -> None:
        """Persist print intent before entering the OS printer submission."""

        self.assert_no_pending_print_audit()
        records = self._build_print_records(
            codes,
            output_path,
            document_copies,
            settings,
            audit_id=audit_id,
            submitted_at=submitted_at,
        )
        self._persist_pending_print(
            records,
            audit_id,
            state="prepared",
        )

    def mark_print_submitted(self, audit_id: str) -> None:
        """Transition a prepared print to known-submitted durable state."""

        pending = self._load_pending_print()
        if pending is None or pending.audit_id != audit_id:
            raise HistoryError(
                "Registrazione stampa preparata non disponibile"
            )
        if pending.state == "submitted":
            return
        self._write_pending_print(
            list(pending.records),
            pending.audit_id,
            "submitted",
        )

    def discard_prepared_print_audit(self) -> bool:
        """Clear an ambiguous prepared job only after operator says not printed."""

        pending = self._load_pending_print()
        if pending is None:
            return False
        if pending.state != "prepared":
            raise HistoryError(
                "Una stampa già confermata come inviata non può essere annullata"
            )
        self._clear_pending_print(pending.audit_id)
        return True

    def _record_print_records(
        self,
        records: list[dict],
        audit_id: str,
    ) -> None:
        missing = records
        if audit_id:
            expected_by_voucher = {
                record["voucher_id"]: record
                for record in records
            }
            existing_by_voucher: dict[str, dict] = {}
            for item in self._items() or ():
                if (
                    item.get("event", "generate") == "print"
                    and item.get("print_job_id") == audit_id
                ):
                    voucher_id = str(
                        item.get("voucher_id", "") or ""
                    )
                    if voucher_id in existing_by_voucher:
                        raise HistoryError(
                            "Cronologia stampa incoerente per il job di recupero"
                        )
                    existing_by_voucher[voucher_id] = item

            for voucher_id, item in existing_by_voucher.items():
                expected = expected_by_voucher.get(voucher_id)
                if expected is None:
                    raise HistoryError(
                        "Identificativo audit già usato per un’altra stampa"
                    )
                comparable = (
                    "timestamp",
                    "output_file",
                    "document_copies",
                    "physical_copies",
                )
                if any(
                    item.get(key) != expected.get(key)
                    for key in comparable
                ):
                    raise HistoryError(
                        "Identificativo audit già usato con dati di stampa diversi"
                    )

            missing = [
                record
                for record in records
                if record["voucher_id"] not in existing_by_voucher
            ]

        self._append(missing)
        self._verify_print_job(records, audit_id)

    def recover_pending_print_audit(
        self,
        *,
        assume_submitted: bool = False,
        clear_pending: bool = True,
    ) -> bool:
        """Complete a pending print audit without voucher plaintext.

        A "prepared" descriptor means the process may have stopped while the
        operating system was receiving the print job. It is intentionally not
        auto-promoted because that would guess whether physical printing
        occurred. The operator can explicitly confirm it via assume_submitted.
        """

        pending = self._load_pending_print()
        if pending is None:
            return False
        self._secret()
        if pending.state == "prepared":
            if not assume_submitted:
                raise HistoryError(
                    "L'esito della stampa pendente è incerto. Confermare "
                    "esplicitamente se il documento è stato stampato oppure "
                    "annullare la registrazione pendente."
                )
            self.mark_print_submitted(pending.audit_id)
            pending = self._load_pending_print()
            if pending is None:
                raise HistoryError(
                    "Registrazione stampa pendente scomparsa durante il recupero"
                )

        self._record_print_records(
            list(pending.records),
            pending.audit_id,
        )
        if clear_pending:
            self._clear_pending_print(pending.audit_id)
        return True

    def record_print(
        self,
        codes: list[str],
        output_path: Path,
        document_copies: int,
        settings: dict,
        *,
        audit_id: str | None = None,
        submitted_at: str | None = None,
        clear_pending: bool = True,
    ) -> None:
        """Record a print already known to have been submitted to Windows.

        clear_pending=False keeps the crash marker until a second durable audit
        (SQLite in 5.0) has committed the same audit_id.
        """

        normalized_audit_id = (
            secrets.token_hex(16)
            if audit_id is None
            else str(audit_id).strip()
        )
        timestamp = (
            str(submitted_at).strip()
            if submitted_at
            else datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        records = self._build_print_records(
            codes,
            output_path,
            document_copies,
            settings,
            audit_id=normalized_audit_id,
            submitted_at=timestamp,
        )
        self._persist_pending_print(
            records,
            normalized_audit_id,
            state="submitted",
        )
        self._record_print_records(
            records,
            normalized_audit_id,
        )
        if clear_pending:
            self._clear_pending_print(normalized_audit_id)

    def _verify_print_job(
        self,
        records: list[dict],
        audit_id: str,
    ) -> None:
        """Verify a print event, including recovery IDs when present."""

        if not audit_id:
            self._verify_event(records, "print")
            return

        wanted = {
            (
                record.get("voucher_id"),
                record.get("output_file"),
                record.get("document_copies"),
                record.get("physical_copies"),
            )
            for record in records
        }
        found = set()
        for item in self._items() or ():
            if (
                item.get("event", "generate") != "print"
                or item.get("print_job_id") != audit_id
            ):
                continue
            found.add(
                (
                    item.get("voucher_id"),
                    item.get("output_file"),
                    item.get("document_copies"),
                    item.get("physical_copies"),
                )
            )
        if found != wanted:
            raise HistoryError(
                "Verifica cronologia non riuscita dopo registrazione stampa"
            )

    def _append(self, records: list[dict]) -> None:
        if not records:
            return
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with exclusive_file_lock(self.lock_path):
                with self.history_path.open("a", encoding="utf-8", newline="\n") as handle:
                    for item in records:
                        handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except (OSError, LockTimeout) as exc:
            raise HistoryError(
                f"Impossibile scrivere la cronologia: {self.history_path}"
            ) from exc

    def _verify_event(self, records: list[dict], event: str) -> None:
        """Verify that the just-written event is readable from persistent history."""
        if not records:
            return
        def identity(item: dict):
            event_id = str(item.get("event_id", "") or "").strip()
            if event_id:
                return ("event_id", event_id)
            return (
                "legacy",
                item.get("voucher_id"),
                item.get("timestamp"),
                item.get("output_file"),
            )

        wanted = {identity(record) for record in records}
        found = set()
        for item in self._items() or ():
            if item.get("event", "generate") == event:
                key = identity(item)
                if key in wanted:
                    found.add(key)
        if found != wanted:
            raise HistoryError(f"Verifica cronologia non riuscita dopo evento '{event}'")
