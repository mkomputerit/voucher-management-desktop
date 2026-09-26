"""Tk maintenance actions kept separate from the main voucher shell.

This mixin owns operator workflows for print-audit recovery, encrypted history
exchange and backup/restore.  It intentionally contains no widget layout for the
main window and no controller/voucher-table behavior.
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

from .backup import BackupError, BackupService
from .database import Database
from .dialogs import ask_password
from .history import HistoryError
from .history_exchange import HistoryExchangeError, HistoryExchangeService
from .utils import format_fingerprint


class DataMaintenanceMixin:
    """UI adapter for maintenance workflows used by ModernVoucherApp."""

    def _backup_service(self) -> BackupService:
        return BackupService(self.paths)

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

    def create_backup(self, *, parent=None) -> None:
        parent = parent or self
        choice = messagebox.askyesnocancel(
            "Crea backup",
            "Proteggere il backup con una password?\n\n"
            "Sì: backup cifrato e autenticato (.vmbk).\n"
            "No: ZIP compatibile non cifrato.",
            parent=parent,
        )
        if choice is None:
            return

        password = None
        suffix = ".vmbk" if choice else ".zip"
        if choice:
            password = ask_password(
                parent,
                title="Password backup",
                prompt="Inserire una password di almeno 12 caratteri.",
                confirm=True,
            )
            if password is None:
                return

        default = (
            f"VoucherManagement-backup-"
            f"{datetime.now().strftime('%Y%m%d-%H%M')}{suffix}"
        )
        filetypes = (
            [("Backup cifrato Voucher Management", "*.vmbk")]
            if choice
            else [("Backup ZIP Voucher Management", "*.zip")]
        )
        target = filedialog.asksaveasfilename(
            parent=parent,
            title="Crea backup",
            defaultextension=suffix,
            initialfile=default,
            filetypes=filetypes,
        )
        if not target:
            return

        service = self._backup_service()

        def completed(result) -> None:
            messagebox.showinfo(
                "Backup completato",
                f"Backup creato correttamente.\n\n{result}",
                parent=parent,
            )

        def failed(exc: Exception) -> None:
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
            lambda: service.create(
                Path(target),
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
            if not messagebox.askyesno(
                "Ripristina backup",
                f"Ripristinare il backup creato il {created}?\n\n"
                "Prima della sostituzione verrà conservata automaticamente "
                "una copia di rollback dei dati attuali.\n\n"
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
                messagebox.showinfo(
                    "Ripristino completato",
                    f"Dati ripristinati.\n\nCopia di sicurezza precedente:\n"
                    f"{rollback}{warning_text}\n\n"
                    "Riavviare Voucher Management.",
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
