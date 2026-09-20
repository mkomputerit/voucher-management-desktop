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

    def status(self, settings: dict | None = None) -> tuple[bool, str]:
        current = self.settings_store.load()
        expected = current.get("history_key_fingerprint", "")
        secret = self.secret_store.get()
        if not expected:
            return False, "Cronologia stampe: fingerprint mancante"
        if not secret:
            return False, "Cronologia stampe: chiave locale mancante"
        if self.fingerprint(secret) != expected:
            return False, "Cronologia stampe: chiave locale non corrispondente"
        return True, "Cronologia stampe: disponibile"

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

    def record_print(self, codes: list[str], output_path: Path, document_copies: int, settings: dict) -> None:
        """Record a print command only after it has been submitted to Windows."""
        secret = self._secret(settings)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        counts = Counter(codes)
        output_name = Path(output_path).name
        records = [
            {
                "event": "print",
                "voucher_id": self._digest(code, secret),
                "timestamp": now,
                "output_file": output_name,
                "document_copies": document_copies,
                "physical_copies": labels * document_copies,
            }
            for code, labels in counts.items()
        ]
        self._append(records)
        self._verify_event(records, "print")

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
        wanted = {(r.get("voucher_id"), r.get("timestamp"), r.get("output_file")) for r in records}
        found = set()
        for item in self._items() or ():
            if item.get("event", "generate") == event:
                key = (item.get("voucher_id"), item.get("timestamp"), item.get("output_file"))
                if key in wanted:
                    found.add(key)
        if found != wanted:
            raise HistoryError(f"Verifica cronologia non riuscita dopo evento '{event}'")
