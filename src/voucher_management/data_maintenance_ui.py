"""Tk maintenance actions kept separate from the main voucher shell.

This mixin owns operator workflows for print-audit recovery, encrypted history
exchange and backup/restore.  It intentionally contains no widget layout for the
main window and no controller/voucher-table behavior.
"""

from __future__ import annotations

import secrets
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox

from .backup import BackupArtifactInfo, BackupError, BackupService
from .database import Database
from .dialogs import ask_password
from .history import HistoryError
from .history_exchange import HistoryExchangeError, HistoryExchangeService
from .legacy_backup_import import (
    execute_legacy_backup_import,
    inspect_legacy_backup,
)
from .legacy_migration import (
    LegacyMigrationError,
    build_legacy_migration_plan,
    execute_legacy_migration,
    legacy_candidates_from_database,
)
from .shared_data_migration import (
    SharedDataMigrationError,
    execute_shared_data_migration,
    shared_target_is_pristine,
    source_has_migratable_data,
)
from .utils import format_fingerprint


class DataMaintenanceMixin:
    """UI adapter for maintenance workflows used by ModernVoucherApp."""

    def _backup_service(self) -> BackupService:
        return BackupService(self.paths)

    @staticmethod
    def _backup_audit_started_at() -> str:
        """Return one canonical UTC timestamp for a backup attempt."""

        return datetime.now(timezone.utc).isoformat()

    def _record_backup_audit(
        self,
        *,
        started_at: str,
        destination: str,
        target: Path,
        artifact: BackupArtifactInfo | None = None,
        error: Exception | None = None,
    ) -> bool:
        """Persist a privacy-safe final backup outcome on the Tk thread.

        Full destination paths and exception messages are intentionally excluded.
        The basename is retained so an operator can correlate an audit row with
        the artifact they selected or received.
        """

        database = getattr(self, "database", None)
        if database is None:
            return False
        try:
            database.record_backup_history(
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
                destination=destination,
                filename=Path(target).name,
                status="SUCCESS" if artifact is not None else "FAILED",
                sha256=None if artifact is None else artifact.sha256,
                backup_format=(
                    None if artifact is None else artifact.backup_format
                ),
                schema_version=(
                    None if artifact is None else artifact.schema_version
                ),
                error_summary=(
                    None if error is None else type(error).__name__
                ),
            )
            return True
        except Exception as exc:
            self.logger.warning(
                "backup_history_write_failed type=%s",
                type(exc).__name__,
            )
            return False

    def request_close(self, *, on_abort=None) -> None:
        """Close safely, creating the configured encrypted recovery snapshot.

        The password exists only for this close attempt. Cancelling either the
        password prompt or a failed-backup decision leaves the application
        open. A running background operation is never interrupted by shutdown.
        """

        if getattr(self, "_background_results", None) is not None:
            messagebox.showinfo(
                "Operazione in corso",
                "Attendere il completamento dell'operazione corrente prima "
                "di chiudere Voucher Management.",
                parent=self,
            )
            if on_abort is not None:
                on_abort()
            return

        if not bool(self.settings.get("backup_on_close", True)):
            self._finish_close(
                close_status="CLOSED",
                backup_status="DISABLED",
            )
            return

        password = ask_password(
            self,
            title="Backup alla chiusura",
            prompt=(
                "Prima di chiudere verrà creato un backup cifrato e "
                "autenticato dell'archivio locale. Inserire la password "
                "del backup (almeno 12 caratteri). La password non viene "
                "salvata."
            ),
        )
        if password is None:
            if on_abort is not None:
                on_abort()
            return

        target = (
            Path(self.paths.automatic_backups)
            / (
                "VoucherManagement-auto-"
                f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.vmbk"
            )
        )
        if on_abort is None:
            self._start_close_backup(target, password)
        else:
            self._start_close_backup(target, password, on_abort=on_abort)

    def _start_close_backup(
        self,
        target: Path,
        password: str,
        *,
        on_abort=None,
    ) -> None:
        """Run one encrypted shutdown backup attempt without blocking Tk."""

        service = self._backup_service()
        started_at = self._backup_audit_started_at()

        def completed(result: BackupArtifactInfo) -> None:
            audited = self._record_backup_audit(
                started_at=started_at,
                destination="SHUTDOWN_AUTO",
                target=target,
                artifact=result,
            )
            if not audited:
                messagebox.showwarning(
                    "Backup completato",
                    "Il backup cifrato è stato creato e verificato, ma il suo "
                    "audit locale non è stato registrato. La chiusura può "
                    "comunque proseguire.",
                    parent=self,
                )
            self._finish_close(
                close_status="CLOSED",
                backup_status=(
                    "SUCCESS" if audited else "SUCCESS_AUDIT_FAILED"
                ),
            )

        def failed(exc: Exception) -> None:
            self._record_backup_audit(
                started_at=started_at,
                destination="SHUTDOWN_AUTO",
                target=target,
                error=exc,
            )
            self.logger.warning(
                "shutdown_backup_failed type=%s",
                type(exc).__name__,
            )
            decision = messagebox.askyesnocancel(
                "Backup di chiusura non riuscito",
                "Non è stato possibile creare e verificare il backup cifrato.\n\n"
                "Sì: riprova il backup.\n"
                "No: chiudi comunque senza un nuovo backup.\n"
                "Annulla: resta nel programma.",
                parent=self,
            )
            if decision is True:
                if on_abort is None:
                    self._start_close_backup(target, password)
                else:
                    self._start_close_backup(
                        target,
                        password,
                        on_abort=on_abort,
                    )
            elif decision is False:
                self._finish_close(
                    close_status="CLOSED_WITHOUT_BACKUP",
                    backup_status="FAILED",
                )
            elif on_abort is not None:
                on_abort()

        started = self._run_background_task(
            "Backup di chiusura…",
            lambda: service.create_verified(
                Path(target),
                password=password,
            ),
            completed,
            failed,
        )
        if not started:
            messagebox.showinfo(
                "Operazione in corso",
                "Il backup di chiusura non può iniziare mentre è attiva "
                "un'altra operazione.",
                parent=self,
            )
            if on_abort is not None:
                on_abort()
            if on_abort is not None:
                on_abort()

    def _finish_close(
        self,
        *,
        close_status: str,
        backup_status: str,
    ) -> None:
        """Record the clean operator-requested close, then tear down Tk."""

        database = getattr(self, "database", None)
        session_uuid = str(getattr(self, "session_uuid", "") or "").strip()
        if database is not None and session_uuid:
            try:
                database.close_application_session(
                    session_uuid=session_uuid,
                    closed_at=datetime.now(timezone.utc).isoformat(),
                    controller_id=getattr(self, "active_controller_id", None),
                    close_status=close_status,
                    backup_status=backup_status,
                )
            except Exception as exc:
                self.logger.warning(
                    "application_session_close_audit_failed type=%s",
                    type(exc).__name__,
                )
        self.destroy()

    def _close_database_for_restore(self) -> None:
        """Release the live SQLite handle before replacing the data tree."""

        database = getattr(self, "database", None)
        if database is None:
            return
        database.close()
        self.database = None

    def _reopen_database_after_failed_restore(self) -> None:
        """Reattach the rolled-back database after an unsuccessful restore."""

        if getattr(self, "database", None) is not None:
            return
        database = Database(self.paths.database)
        try:
            database.initialize()
            database.integrity_check()
        except Exception:
            database.close()
            raise
        self.database = database

    def _dialog_busy_scope(self, parent):
        """Return a callback that disables a modal caller while work runs."""

        if parent is None or parent is self:
            return None

        def set_busy(busy: bool) -> None:
            try:
                if not parent.winfo_exists():
                    return
                parent.attributes("-disabled", 1 if busy else 0)
            except (AttributeError, tk.TclError):
                # Non-Windows window managers may not expose -disabled.
                # The application coordinator still prevents overlap.
                return

        return set_busy

    def _history_exchange_service(self) -> HistoryExchangeService:
        return HistoryExchangeService(self.history)

    @staticmethod
    def _history_exchange_fingerprint(value: str) -> str:
        return format_fingerprint(value)

    def recover_pending_print_audit(
        self,
        *,
        parent=None,
    ) -> None:
        parent = parent or self

        try:
            pending_state = self.history.pending_print_state()
        except HistoryError as exc:
            messagebox.showerror(
                "Registrazione stampa",
                str(exc),
                parent=parent,
            )
            return

        if pending_state == "prepared":
            printed = messagebox.askyesnocancel(
                "Esito stampa da verificare",
                "La precedente stampa si è interrotta mentre Windows poteva "
                "stare ricevendo il documento.\n\n"
                "Il documento è stato effettivamente stampato?\n\n"
                "Sì: registra la stampa nello storico.\n"
                "No: annulla la registrazione pendente.\n"
                "Annulla: non modificare nulla.",
                parent=parent,
            )
            if printed is None:
                return
            if printed is False:
                def discarded(cleared) -> None:
                    if cleared:
                        self.populate()
                    messagebox.showinfo(
                        "Registrazione stampa",
                        "Registrazione pendente annullata. È ora possibile "
                        "ripetere la stampa.",
                        parent=parent,
                    )

                self._run_background_task(
                    "Annullamento stampa pendente…",
                    self.history.discard_prepared_print_audit,
                    discarded,
                    lambda exc: messagebox.showerror(
                        "Registrazione stampa",
                        str(exc)
                        if isinstance(exc, HistoryError)
                        else "Annullamento della stampa pendente non riuscito.",
                        parent=parent,
                    ),
                    busy_scope=self._dialog_busy_scope(parent),
                )
                return

            if callable(
                getattr(self, "_record_pending_print_sqlite_and_finalize", None)
            ):
                recovery_worker = lambda: self.history.recover_pending_print_audit(
                    assume_submitted=True,
                    clear_pending=False,
                )
            else:
                recovery_worker = lambda: self.history.recover_pending_print_audit(
                    assume_submitted=True
                )
        else:
            if callable(
                getattr(self, "_record_pending_print_sqlite_and_finalize", None)
            ):
                recovery_worker = lambda: self.history.recover_pending_print_audit(
                    clear_pending=False
                )
            else:
                recovery_worker = self.history.recover_pending_print_audit

        def completed(recovered) -> None:
            if recovered:
                sqlite_recovery = getattr(
                    self,
                    "_record_pending_print_sqlite_and_finalize",
                    None,
                )
                if callable(sqlite_recovery):
                    try:
                        sqlite_recovery()
                    except Exception as exc:
                        self.logger.warning(
                            "pending_sqlite_print_recovery_failed type=%s",
                            type(exc).__name__,
                        )
                        messagebox.showerror(
                            "Registrazione stampa",
                            "Lo storico HMAC è stato verificato, ma l'archivio "
                            "SQLite non è stato completato. La registrazione "
                            "pendente resta protetta e può essere ritentata.",
                            parent=parent,
                        )
                        return
                self.populate()
                messagebox.showinfo(
                    "Registrazione stampa",
                    "La stampa pendente è stata registrata correttamente "
                    "nello storico.",
                    parent=parent,
                )
            else:
                messagebox.showinfo(
                    "Registrazione stampa",
                    "Non risultano stampe pendenti da recuperare.",
                    parent=parent,
                )

        def failed(exc: Exception) -> None:
            detail = (
                str(exc)
                if isinstance(exc, HistoryError)
                else "Recupero della stampa pendente non riuscito."
            )
            messagebox.showerror(
                "Registrazione stampa",
                detail,
                parent=parent,
            )

        self._run_background_task(
            "Recupero stampa pendente…",
            recovery_worker,
            completed,
            failed,
            busy_scope=self._dialog_busy_scope(parent),
        )

    def export_history_exchange(
        self,
        *,
        parent=None,
    ) -> None:
        parent = parent or self
        state = self.history.identity_state()
        if not state.ready:
            messagebox.showerror(
                "Esporta cronologia",
                "L'identità della cronologia locale non è verificata. "
                "Usare prima 'Verifica / recupera identità…'.",
                parent=parent,
            )
            return

        password = ask_password(
            parent,
            title="Password esportazione",
            prompt=(
                "Inserire una password di almeno 12 caratteri per proteggere "
                "cronologia e chiave portabile."
            ),
            confirm=True,
        )
        if password is None:
            return

        default = (
            "VoucherManagement-history-"
            f"{datetime.now().strftime('%Y%m%d-%H%M')}.vmhx"
        )
        target = filedialog.asksaveasfilename(
            parent=parent,
            title="Esporta cronologia",
            defaultextension=".vmhx",
            initialfile=default,
            filetypes=[
                (
                    "Cronologia Voucher Management cifrata",
                    "*.vmhx",
                )
            ],
        )
        if not target:
            return

        service = self._history_exchange_service()

        def completed(result) -> None:
            messagebox.showinfo(
                "Esporta cronologia",
                "Cronologia esportata correttamente.\n\n"
                f"{result}",
                parent=parent,
            )

        def failed(exc: Exception) -> None:
            detail = (
                str(exc)
                if isinstance(exc, (HistoryExchangeError, ValueError))
                else "Esportazione cronologia non riuscita."
            )
            messagebox.showerror(
                "Esporta cronologia",
                detail,
                parent=parent,
            )

        self._run_background_task(
            "Esportazione cronologia…",
            lambda: service.export(Path(target), password),
            completed,
            failed,
            busy_scope=self._dialog_busy_scope(parent),
        )

    def import_history_exchange(
        self,
        *,
        parent=None,
    ) -> None:
        parent = parent or self
        source = filedialog.askopenfilename(
            parent=parent,
            title="Importa cronologia",
            filetypes=[
                (
                    "Cronologia Voucher Management cifrata",
                    "*.vmhx",
                )
            ],
        )
        if not source:
            return

        password = ask_password(
            parent,
            title="Password importazione",
            prompt="Inserire la password del pacchetto cronologia.",
        )
        if password is None:
            return

        service = self._history_exchange_service()
        source_path = Path(source)

        def prepare_failed(exc: Exception) -> None:
            detail = (
                str(exc)
                if isinstance(exc, (HistoryExchangeError, ValueError))
                else "Impossibile validare il pacchetto cronologia."
            )
            messagebox.showerror(
                "Importa cronologia",
                detail,
                parent=parent,
            )

        def prepared(plan) -> None:
            fingerprint = self._history_exchange_fingerprint(
                plan.incoming_fingerprint
            )
            summary = (
                f"Fingerprint: {fingerprint}\n\n"
                f"Eventi nel pacchetto: {plan.incoming_events}\n"
                f"Nuovi eventi: {plan.new_events}\n"
                f"Già presenti: {plan.duplicate_events}\n"
                f"Conflitti: {plan.conflict_events}"
            )

            if plan.conflict_events:
                messagebox.showerror(
                    "Importa cronologia",
                    summary
                    + "\n\nIl pacchetto contiene eventi di stampa in "
                    "conflitto con la cronologia locale. Nessun dato è stato "
                    "modificato.",
                    parent=parent,
                )
                return

            adoption_text = ""
            if plan.can_adopt_identity:
                adoption_text = (
                    "\n\nQuesta postazione non contiene eventi di cronologia "
                    "utili. Per importare verrà sostituita la sua identità HMAC "
                    "locale con quella del pacchetto."
                )

            if not messagebox.askyesno(
                "Importa cronologia",
                summary
                + adoption_text
                + "\n\nProcedere con il merge?",
                parent=parent,
            ):
                return

            def applied(added) -> None:
                self.settings = self.settings_store.load()
                self._history_error_shown = False
                self.populate()
                messagebox.showinfo(
                    "Importa cronologia",
                    "Merge completato.\n\n"
                    f"Eventi aggiunti: {added}\n"
                    f"Eventi già presenti: {plan.duplicate_events}",
                    parent=parent,
                )

            def apply_failed(exc: Exception) -> None:
                detail = (
                    str(exc)
                    if isinstance(exc, HistoryExchangeError)
                    else "Importazione cronologia non riuscita."
                )
                messagebox.showerror(
                    "Importa cronologia",
                    detail,
                    parent=parent,
                )

            self._run_background_task(
                "Merge cronologia…",
                lambda: service.apply_import(
                    plan,
                    adopt_identity=plan.can_adopt_identity,
                ),
                applied,
                apply_failed,
                busy_scope=self._dialog_busy_scope(parent),
            )

        self._run_background_task(
            "Verifica pacchetto cronologia…",
            lambda: service.prepare_import(source_path, password),
            prepared,
            prepare_failed,
            busy_scope=self._dialog_busy_scope(parent),
        )

    def import_legacy_backup(self, *, parent=None) -> None:
        """Import verified print history directly from a pre-SQLite ZIP backup."""

        parent = parent or self
        source = filedialog.askopenfilename(
            parent=parent,
            title="Importa backup precedente",
            filetypes=[
                ("Backup ZIP Voucher Management", "*.zip"),
                ("File ZIP", "*.zip"),
            ],
        )
        if not source:
            return

        source_path = Path(source)
        validator = self._backup_service()

        def inspected(info) -> None:
            summary = (
                f"Backup: {source_path.name}\n"
                f"Creato: {info.created_utc or 'data sconosciuta'}\n\n"
                f"Eventi cronologia: {info.history_rows}\n"
                f"Generazioni PDF: {info.generated_rows}\n"
                f"Stampe fisiche: {info.print_rows}\n"
                f"Voucher storici: {info.unique_history_vouchers}\n"
                f"PDF presenti: {info.pdf_files}\n"
                f"Sequenze candidate recuperate dai PDF: "
                f"{info.recovered_codes}\n"
                f"Voucher correlati con HMAC: "
                f"{info.matched_history_vouchers}\n"
                f"Sequenze PDF ignorate perché senza HMAC: "
                f"{info.unmatched_pdf_codes}\n"
                f"Eventi storici non correlati: "
                f"{info.unmatched_history_vouchers}\n\n"
                f"SHA-256 sorgente:\n{info.source_sha256}\n\n"
                "Il formato ZIP precedente non è autenticato contro una "
                "fonte esterna: importare solo archivi di provenienza nota. "
                "L'importazione non sovrascrive la configurazione corrente. "
                "Prima di modificare il database verrà creato un backup "
                "cifrato di sicurezza della 5.x corrente.\n\n"
                "Procedere?"
            )
            if not messagebox.askyesno(
                "Importa backup precedente",
                summary,
                parent=parent,
            ):
                return

            password = ask_password(
                parent,
                title="Backup di sicurezza pre-importazione",
                prompt=(
                    "Inserire una password di almeno 12 caratteri per il "
                    "backup cifrato della situazione corrente."
                ),
                confirm=True,
            )
            if password is None:
                return

            default = (
                "VoucherManagement-pre-import-"
                f"{datetime.now().strftime('%Y%m%d-%H%M')}.vmbk"
            )
            target = filedialog.asksaveasfilename(
                parent=parent,
                title="Backup di sicurezza pre-importazione",
                defaultextension=".vmbk",
                initialfile=default,
                filetypes=[
                    (
                        "Backup cifrato Voucher Management",
                        "*.vmbk",
                    )
                ],
            )
            if not target:
                return

            imported_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            migration_uuid = secrets.token_hex(16)
            database_path = Path(self.paths.database)
            app_paths = self.paths
            preferred_controller_id = getattr(
                self,
                "active_controller_id",
                None,
            )

            def worker():
                import_db = Database(database_path)
                try:
                    import_db.initialize()
                    import_db.integrity_check()
                    return execute_legacy_backup_import(
                        database=import_db,
                        live_backup_service=BackupService(app_paths),
                        source=source_path,
                        safety_backup_destination=Path(target),
                        safety_backup_password=password,
                        imported_at=imported_at,
                        migration_uuid=migration_uuid,
                        preferred_controller_id=preferred_controller_id,
                    )
                finally:
                    import_db.close()

            def completed(result) -> None:
                self.populate()
                refresh_report = getattr(
                    self,
                    "_refresh_report_summary",
                    None,
                )
                if refresh_report is not None:
                    refresh_report()
                refresh_backup = getattr(
                    self,
                    "_refresh_backup_summary",
                    None,
                )
                if refresh_backup is not None:
                    refresh_backup()
                refresh_legacy = getattr(
                    self,
                    "_refresh_legacy_history_summary",
                    None,
                )
                if refresh_legacy is not None:
                    refresh_legacy()

                messagebox.showinfo(
                    "Importazione completata",
                    "Backup precedente importato nel database 5.x.\n\n"
                    f"Eventi analizzati: "
                    f"{result.evidence.total_rows}\n"
                    f"Associati con certezza: "
                    f"{result.evidence.resolved_rows}\n"
                    f"Ambigui conservati: "
                    f"{result.evidence.ambiguous_rows}\n"
                    f"Non associati conservati: "
                    f"{result.evidence.unresolved_rows}\n"
                    f"Stampe materializzate/verificate: "
                    f"{result.materialization.print_rows}\n"
                    f"Voucher esistenti riutilizzati: "
                    f"{result.reused_vouchers}\n"
                    f"Voucher storici creati: "
                    f"{result.historical_vouchers_created}\n"
                    f"PDF importati: {result.pdfs_copied}\n"
                    f"PDF già presenti: "
                    f"{result.pdfs_already_present}\n\n"
                    f"Backup di sicurezza della 5.x:\n"
                    f"{result.safety_backup_path}\n\n"
                    "Consigliato: dopo aver verificato l'importazione, crea "
                    "un nuovo backup .vmbk della base dati aggiornata.",
                    parent=parent,
                )

            def failed(exc: Exception) -> None:
                detail = (
                    str(exc)
                    if isinstance(
                        exc,
                        (LegacyMigrationError, BackupError),
                    )
                    else "Importazione del backup precedente non riuscita."
                )
                messagebox.showerror(
                    "Importa backup precedente",
                    detail,
                    parent=parent,
                )

            self._run_background_task(
                "Importazione backup precedente…",
                worker,
                completed,
                failed,
                busy_scope=self._dialog_busy_scope(parent),
            )

        def inspection_failed(exc: Exception) -> None:
            detail = (
                str(exc)
                if isinstance(
                    exc,
                    (LegacyMigrationError, BackupError),
                )
                else "Impossibile analizzare il backup precedente."
            )
            messagebox.showerror(
                "Importa backup precedente",
                detail,
                parent=parent,
            )

        self._run_background_task(
            "Analisi backup precedente…",
            lambda: inspect_legacy_backup(
                source_path,
                validator=validator,
            ),
            inspected,
            inspection_failed,
            busy_scope=self._dialog_busy_scope(parent),
        )

    def migrate_legacy_history(self, *, parent=None) -> None:
        """Run the explicit 4.x history migration after operator review.

        Candidate voucher identities are snapshotted on the Tk thread from the
        already-open database. HMAC planning and the safety-backed apply run in
        background workers; the worker opens its own SQLite connection because
        the Tk connection must never cross thread boundaries.
        """

        parent = parent or self
        try:
            fingerprint, history_key = self.history.verified_identity_material()
            candidates = legacy_candidates_from_database(self.database)
        except Exception as exc:
            detail = (
                str(exc)
                if isinstance(exc, (HistoryError, LegacyMigrationError))
                else "Impossibile preparare la migrazione dello storico."
            )
            messagebox.showerror(
                "Migrazione storico 4.x",
                detail,
                parent=parent,
            )
            return

        history_path = Path(self.paths.history)

        def planned(plan) -> None:
            if plan.total_rows == 0:
                messagebox.showinfo(
                    "Migrazione storico 4.x",
                    "Non risultano eventi nello storico 4.x da migrare.",
                    parent=parent,
                )
                return

            summary = (
                f"Eventi analizzati: {plan.total_rows}\n"
                f"Associati con certezza: {len(plan.resolved)}\n"
                f"Con più possibili corrispondenze: {len(plan.ambiguous)}\n"
                f"Senza una corrispondenza disponibile: "
                f"{len(plan.unresolved)}\n\n"
                "Gli eventi che non possono essere associati con certezza "
                "verranno comunque conservati nello storico, ma non verranno "
                "usati per ricostruire stampe o altri dati operativi. "
                "Nessuna associazione verrà scelta automaticamente.\n\n"
                "Prima della migrazione verrà creato un backup cifrato "
                "obbligatorio. Procedere?"
            )
            if not messagebox.askyesno(
                "Migrazione storico 4.x",
                summary,
                parent=parent,
            ):
                return

            password = ask_password(
                parent,
                title="Backup pre-migrazione",
                prompt=(
                    "Inserire una password di almeno 12 caratteri per il "
                    "backup di sicurezza pre-migrazione."
                ),
                confirm=True,
            )
            if password is None:
                return

            default = (
                "VoucherManagement-pre-migration-"
                f"{datetime.now().strftime('%Y%m%d-%H%M')}.vmbk"
            )
            target = filedialog.asksaveasfilename(
                parent=parent,
                title="Backup di sicurezza pre-migrazione",
                defaultextension=".vmbk",
                initialfile=default,
                filetypes=[
                    (
                        "Backup cifrato Voucher Management",
                        "*.vmbk",
                    )
                ],
            )
            if not target:
                return

            migration_uuid = secrets.token_hex(16)
            applied_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            materialized_at = applied_at
            database_path = Path(self.paths.database)
            app_paths = self.paths

            def worker():
                migration_db = Database(database_path)
                try:
                    migration_db.initialize()
                    migration_db.integrity_check()
                    return execute_legacy_migration(
                        database=migration_db,
                        backup_service=BackupService(app_paths),
                        backup_destination=Path(target),
                        backup_password=password,
                        plan=plan,
                        migration_uuid=migration_uuid,
                        applied_at=applied_at,
                        materialized_at=materialized_at,
                    )
                finally:
                    migration_db.close()

            def completed(result) -> None:
                self.populate()
                messagebox.showinfo(
                    "Migrazione storico 4.x",
                    "Migrazione completata.\n\n"
                    f"Eventi analizzati: {result.evidence.total_rows}\n"
                    f"Associati con certezza: "
                    f"{result.evidence.resolved_rows}\n"
                    f"Con più possibili corrispondenze, conservati: "
                    f"{result.evidence.ambiguous_rows}\n"
                    f"Senza corrispondenza disponibile, conservati: "
                    f"{result.evidence.unresolved_rows}\n"
                    f"Eventi PDF materializzati: "
                    f"{result.materialization.generated_events}\n"
                    f"Stampe materializzate/verificate: "
                    f"{result.materialization.print_rows}\n\n"
                    f"Backup di sicurezza:\n{result.backup_path}\n\n"
                    "Lo storico 4.x originale non è stato modificato.",
                    parent=parent,
                )

            def failed(exc: Exception) -> None:
                detail = (
                    str(exc)
                    if isinstance(exc, LegacyMigrationError)
                    else "Migrazione dello storico non riuscita."
                )
                messagebox.showerror(
                    "Migrazione storico 4.x",
                    detail,
                    parent=parent,
                )

            self._run_background_task(
                "Migrazione storico 4.x…",
                worker,
                completed,
                failed,
                busy_scope=self._dialog_busy_scope(parent),
            )

        def planning_failed(exc: Exception) -> None:
            detail = (
                str(exc)
                if isinstance(exc, LegacyMigrationError)
                else "Analisi dello storico 4.x non riuscita."
            )
            messagebox.showerror(
                "Migrazione storico 4.x",
                detail,
                parent=parent,
            )

        self._run_background_task(
            "Analisi storico 4.x…",
            lambda: build_legacy_migration_plan(
                history_path=history_path,
                expected_fingerprint=fingerprint,
                secret=history_key,
                candidates=candidates,
            ),
            planned,
            planning_failed,
            busy_scope=self._dialog_busy_scope(parent),
        )

    def migrate_per_user_data_to_shared(self, *, parent=None) -> None:
        """Move this Windows user's previous data into shared ProgramData.

        The operation is available only in installer-controlled shared mode.
        It never merges over an already-used shared archive: the target must
        still contain only bootstrap schema/history-identity artifacts.
        """

        parent = parent or self
        if not getattr(self.paths, "shared_mode", False):
            messagebox.showinfo(
                "Archivio condiviso",
                "Questa installazione utilizza ancora dati per utente.",
                parent=parent,
            )
            return

        source_root = Path(self.paths.per_user_root)
        if not source_has_migratable_data(source_root):
            messagebox.showinfo(
                "Migrazione dati utente",
                "Non risultano dati precedenti nel profilo Windows corrente.",
                parent=parent,
            )
            return

        try:
            pristine = shared_target_is_pristine(self.database, self.paths)
        except Exception as exc:
            self.logger.warning(
                "shared_migration_preflight_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Migrazione dati utente",
                "Impossibile verificare in sicurezza l'archivio condiviso.",
                parent=parent,
            )
            return

        if not pristine:
            messagebox.showerror(
                "Migrazione dati utente",
                "L'archivio condiviso contiene già dati operativi. "
                "Per evitare sovrascritture, la migrazione del profilo utente "
                "non può essere eseguita su questa installazione.",
                parent=parent,
            )
            return

        if not messagebox.askyesno(
            "Migrazione dati utente",
            "Sono stati trovati dati della precedente installazione nel "
            "profilo Windows corrente. Verranno trasferiti nell'archivio "
            "condiviso di questo PC tramite un backup cifrato e verificato.\n\n"
            "La copia originale nel profilo utente non verrà modificata. "
            "Al termine Voucher Management verrà chiuso.\n\n"
            "Procedere?",
            parent=parent,
        ):
            return

        password = ask_password(
            parent,
            title="Backup pre-migrazione",
            prompt=(
                "Inserire una password di almeno 12 caratteri per il backup "
                "di sicurezza dei dati precedenti."
            ),
            confirm=True,
        )
        if password is None:
            return

        default = (
            "VoucherManagement-user-migration-"
            f"{datetime.now().strftime('%Y%m%d-%H%M')}.vmbk"
        )
        target = filedialog.asksaveasfilename(
            parent=parent,
            title="Backup dei dati utente precedenti",
            defaultextension=".vmbk",
            initialfile=default,
            filetypes=[
                ("Backup cifrato Voucher Management", "*.vmbk"),
            ],
        )
        if not target:
            return

        try:
            # The target database must be released before BackupService.restore
            # replaces ProgramData/data on Windows.
            DataMaintenanceMixin._close_database_for_restore(self)
        except Exception as exc:
            self.logger.error(
                "database_close_before_shared_migration_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Migrazione dati utente",
                "Impossibile chiudere il database condiviso prima della migrazione.",
                parent=parent,
            )
            return

        def completed(result) -> None:
            audit_note = (
                ""
                if result.backup_audit_recorded
                else (
                    "\n\nAttenzione: il trasferimento è riuscito, ma "
                    "l'audit locale del backup sorgente non è stato registrato."
                )
            )
            messagebox.showinfo(
                "Migrazione completata",
                "I dati del profilo Windows sono stati trasferiti "
                "nell'archivio condiviso.\n\n"
                f"Backup sorgente verificato:\n{result.backup_path}\n\n"
                f"Rollback del precedente ProgramData:\n"
                f"{result.rollback_path}\n\n"
                "La copia originale nel profilo utente è rimasta invariata. "
                f"Riavviare Voucher Management.{audit_note}",
                parent=parent,
            )
            self.destroy()

        def failed(exc: Exception) -> None:
            try:
                DataMaintenanceMixin._reopen_database_after_failed_restore(self)
            except Exception as reopen_exc:
                self.logger.error(
                    "database_reopen_after_shared_migration_failed type=%s",
                    type(reopen_exc).__name__,
                )
                messagebox.showerror(
                    "Migrazione dati utente",
                    "La migrazione non è riuscita e il database condiviso non "
                    "può essere riaperto in sicurezza. Chiudere e riavviare "
                    "Voucher Management.",
                    parent=parent,
                )
                return

            detail = (
                str(exc)
                if isinstance(exc, SharedDataMigrationError)
                else "Migrazione dei dati utente non riuscita."
            )
            messagebox.showerror(
                "Migrazione dati utente",
                detail,
                parent=parent,
            )

        started = self._run_background_task(
            "Migrazione dati utente…",
            lambda: execute_shared_data_migration(
                source_root=source_root,
                target_paths=self.paths,
                backup_destination=Path(target),
                backup_password=password,
            ),
            completed,
            failed,
            busy_scope=self._dialog_busy_scope(parent),
        )
        if not started:
            try:
                DataMaintenanceMixin._reopen_database_after_failed_restore(self)
            except Exception as exc:
                self.logger.error(
                    "database_reopen_after_shared_migration_cancelled type=%s",
                    type(exc).__name__,
                )

    def create_backup(self, *, parent=None) -> None:
        """Create the normal 5.0 backup only through the encrypted format."""

        parent = parent or self
        password = ask_password(
            parent,
            title="Password backup",
            prompt=(
                "Inserire una password di almeno 12 caratteri per proteggere "
                "il backup cifrato e autenticato."
            ),
            confirm=True,
        )
        if password is None:
            return

        default = (
            "VoucherManagement-backup-"
            f"{datetime.now().strftime('%Y%m%d-%H%M')}.vmbk"
        )
        target = filedialog.asksaveasfilename(
            parent=parent,
            title="Crea backup cifrato",
            defaultextension=".vmbk",
            initialfile=default,
            filetypes=[
                ("Backup cifrato Voucher Management", "*.vmbk"),
            ],
        )
        if not target:
            return

        service = self._backup_service()
        started_at = self._backup_audit_started_at()
        target_path = Path(target)

        def completed(result: BackupArtifactInfo) -> None:
            audited = self._record_backup_audit(
                started_at=started_at,
                destination="MANUAL",
                target=target_path,
                artifact=result,
            )
            audit_note = (
                ""
                if audited
                else (
                    "\n\nAttenzione: il file è valido, ma l'audit locale "
                    "del backup non è stato registrato."
                )
            )
            messagebox.showinfo(
                "Backup completato",
                f"Backup creato correttamente.\n\n{result.path}{audit_note}",
                parent=parent,
            )

        def failed(exc: Exception) -> None:
            self._record_backup_audit(
                started_at=started_at,
                destination="MANUAL",
                target=target_path,
                error=exc,
            )
            detail = (
                str(exc)
                if isinstance(exc, BackupError)
                else "Creazione backup non riuscita."
            )
            messagebox.showerror(
                "Backup",
                detail,
                parent=parent,
            )

        self._run_background_task(
            "Creazione backup…",
            lambda: service.create_verified(
                target_path,
                password=password,
            ),
            completed,
            failed,
            busy_scope=self._dialog_busy_scope(parent),
        )

    def restore_backup(self, *, parent=None) -> None:
        parent = parent or self
        source = filedialog.askopenfilename(
            parent=parent,
            title="Ripristina backup",
            filetypes=[
                ("Backup Voucher Management", "*.vmbk *.zip"),
                ("Backup cifrato", "*.vmbk"),
                ("Backup ZIP legacy", "*.zip"),
            ],
        )
        if not source:
            return

        service = self._backup_service()
        source_path = Path(source)
        password = None
        if service.is_encrypted_backup(source_path):
            password = ask_password(
                parent,
                title="Password backup",
                prompt="Inserire la password del backup cifrato.",
            )
            if password is None:
                return

        def validation_failed(exc: Exception) -> None:
            detail = (
                str(exc)
                if isinstance(exc, BackupError)
                else "Impossibile validare il backup."
            )
            messagebox.showerror(
                "Ripristino",
                detail,
                parent=parent,
            )

        def validated(manifest) -> None:
            created = manifest.get(
                "created_utc",
                "data sconosciuta",
            )
            legacy_without_sqlite = manifest.get("sqlite_snapshot") is None
            migration_note = (
                "\n\nQuesto backup appartiene a una versione precedente e "
                "non contiene ancora il database SQLite 5.x. Verranno "
                "ripristinati configurazione, cronologia stampe, PDF, logo e "
                "chiave della cronologia. Dopo il riavvio sarà necessario "
                "sincronizzare il controller e importare la cronologia "
                "precedente nel database 5.x."
                if legacy_without_sqlite
                else ""
            )
            if not messagebox.askyesno(
                "Ripristina backup",
                f"Ripristinare il backup creato il {created}?\n\n"
                "Prima della sostituzione verrà conservata automaticamente "
                "una copia di rollback dei dati attuali."
                f"{migration_note}\n\n"
                "Dopo il ripristino il programma verrà chiuso.",
                parent=parent,
            ):
                return

            def restored(rollback) -> None:
                warnings = service.consume_restore_warnings()
                warning_text = ""
                if warnings:
                    warning_text = (
                        "\n\nAttenzione: alcuni logo legacy non rispettano i "
                        "limiti correnti e non sono stati ripristinati:\n- "
                        + "\n- ".join(warnings)
                        + "\n\nSe necessario, selezionare nuovamente un logo "
                        "dalle impostazioni."
                    )
                next_steps = (
                    "\n\nPassi successivi:\n"
                    "1. Riavviare Voucher Management.\n"
                    "2. Ricollegare e sincronizzare il controller UniFi.\n"
                    "3. Aprire Impostazioni > Backup e scegliere "
                    "'Importa cronologia stampe precedente…'.\n\n"
                    "Solo dopo questa importazione lo storico precedente sarà "
                    "materializzato nel database 5.x e nei report."
                    if legacy_without_sqlite
                    else "\n\nRiavviare Voucher Management."
                )
                messagebox.showinfo(
                    "Ripristino completato",
                    f"Dati ripristinati.\n\nCopia di sicurezza precedente:\n"
                    f"{rollback}{warning_text}{next_steps}",
                    parent=parent,
                )
                self.destroy()

            def restore_failed(exc: Exception) -> None:
                try:
                    DataMaintenanceMixin._reopen_database_after_failed_restore(self)
                except Exception as reopen_exc:
                    self.logger.error(
                        "database_reopen_after_restore_failed type=%s",
                        type(reopen_exc).__name__,
                    )
                    messagebox.showerror(
                        "Ripristino",
                        "Il ripristino non è riuscito e il database locale "
                        "non può essere riaperto in sicurezza. Chiudere e "
                        "riavviare Voucher Management.",
                        parent=parent,
                    )
                    return

                detail = (
                    str(exc)
                    if isinstance(exc, BackupError)
                    else "Ripristino backup non riuscito."
                )
                messagebox.showerror(
                    "Ripristino",
                    detail,
                    parent=parent,
                )

            try:
                # SQLite is opened on the Tk thread. Close it here before the
                # worker can replace data/voucher_management.db on Windows.
                DataMaintenanceMixin._close_database_for_restore(self)
            except Exception as exc:
                self.logger.error(
                    "database_close_before_restore_failed type=%s",
                    type(exc).__name__,
                )
                messagebox.showerror(
                    "Ripristino",
                    "Impossibile chiudere il database locale prima del "
                    "ripristino.",
                    parent=parent,
                )
                return

            started = self._run_background_task(
                "Ripristino backup…",
                lambda: service.restore(
                    source_path,
                    password=password,
                ),
                restored,
                restore_failed,
                busy_scope=self._dialog_busy_scope(parent),
            )
            if not started:
                try:
                    DataMaintenanceMixin._reopen_database_after_failed_restore(self)
                except Exception as exc:
                    self.logger.error(
                        "database_reopen_after_restore_cancelled type=%s",
                        type(exc).__name__,
                    )

        def validate_worker():
            if password is None:
                return service.validate(source_path)
            return service.validate_encrypted(
                source_path,
                password,
            )

        self._run_background_task(
            "Verifica backup…",
            validate_worker,
            validated,
            validation_failed,
            busy_scope=self._dialog_busy_scope(parent),
        )
