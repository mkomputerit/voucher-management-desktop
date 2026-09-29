"""Tk adapter for voucher creation and uncertain-mutation protection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox

from .dialogs import CreateDialog
from .mutation_guard import CreateMutationGuardError
from .sync_store import persist_creation_result_to_path
from .workflows import create_vouchers_and_refresh


class VoucherCreationMixin:
    """Non-layout voucher creation workflow with durable anti-repeat safety."""

    def create(self):
        if not self.client:
            messagebox.showinfo(
                "UniFi",
                "Connettersi prima al controller UniFi",
                parent=self,
            )
            return
        if self.create_guard.pending:
            messagebox.showwarning(
                "Creazione sospesa",
                "Una precedente creazione ha un esito da verificare. "
                "Per evitare voucher duplicati, eseguire prima Aggiorna e "
                "controllare l'elenco restituito dal controller.",
                parent=self,
            )
            return

        dialog = CreateDialog(self)
        if not dialog.result:
            return

        try:
            self.create_guard.begin()
        except CreateMutationGuardError as exc:
            self.logger.warning(
                "create_guard_begin_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Creazione sospesa",
                "Non è possibile attivare la protezione anti-ripetizione. "
                "Nessuna richiesta di creazione è stata inviata.",
                parent=self,
            )
            return

        client = self.client
        cached = list(self.vouchers)
        params = dict(dialog.result)
        is_nominal = bool(params.pop("is_nominal", False))
        controller_id = getattr(self, "active_controller_id", None)
        database_path = (
            Path(self.paths.database)
            if controller_id is not None
            else None
        )

        def worker():
            outcome = create_vouchers_and_refresh(
                client,
                cached,
                params,
            )
            persistence_error = None
            if controller_id is not None and database_path is not None:
                try:
                    persist_creation_result_to_path(
                        database_path,
                        controller_id=controller_id,
                        vouchers=list(outcome.vouchers),
                        created=list(outcome.created),
                        observed_at=datetime.now(timezone.utc).isoformat(),
                        is_nominal=is_nominal,
                        snapshot_complete=outcome.refresh_error is None,
                    )
                except Exception as exc:
                    persistence_error = exc
            return outcome, persistence_error

        def completed(result) -> None:
            outcome, persistence_error = result
            self.vouchers = list(outcome.vouchers)
            self.controller_snapshot_fresh = outcome.refresh_error is None

            if persistence_error is not None:
                self.logger.error(
                    "create_reporting_persistence_failed type=%s",
                    type(persistence_error).__name__,
                )

            if outcome.uncertain_error is not None:
                self.checked_ids.clear()
                self.populate()
                if persistence_error is not None:
                    messagebox.showwarning(
                        "Archivio locale non aggiornato",
                        "La lettura del Controller è riuscita, ma non è stato "
                        "possibile aggiornare lo storico locale. I report "
                        "potrebbero essere incompleti finché la sincronizzazione "
                        "non viene ripetuta.",
                        parent=self,
                    )
                if outcome.refresh_error is None:
                    detail = (
                        "L'elenco è stato riletto dal controller, ma in una "
                        "configurazione multi-postazione non è sicuro attribuire "
                        "automaticamente eventuali nuovi voucher a questa richiesta."
                    )
                else:
                    detail = (
                        "Non è stato possibile rileggere l'elenco dal controller."
                    )
                messagebox.showwarning(
                    "Esito creazione da verificare",
                    "La richiesta di creazione potrebbe essere stata completata "
                    "dal controller, ma la risposta non è arrivata in modo "
                    "definitivo. Non ripetere la creazione.\n\n"
                    f"{detail}\n\n"
                    "La creazione resta bloccata finché non viene eseguito "
                    "Aggiorna con successo e verificato l'elenco.",
                    parent=self,
                )
                return

            try:
                self.create_guard.clear()
            except CreateMutationGuardError as exc:
                self.logger.warning(
                    "create_guard_clear_failed type=%s",
                    type(exc).__name__,
                )
                messagebox.showwarning(
                    "Voucher creati",
                    "La creazione è stata completata, ma il blocco "
                    "anti-ripetizione non può essere rimosso automaticamente. "
                    "Eseguire Aggiorna prima di una nuova creazione.",
                    parent=self,
                )

            self.checked_ids = {
                voucher.id
                for voucher in outcome.created
            }
            self.filter_var.set("Da stampare")
            self.populate()

            if persistence_error is not None:
                messagebox.showwarning(
                    "Classificazione non registrata",
                    "I voucher sono stati creati sul Controller, ma la "
                    "classificazione locale per la reportistica non è stata "
                    "salvata. Non considerare affidabili i report di questi "
                    "voucher finché il problema non viene corretto.",
                    parent=self,
                )

            if outcome.refresh_error is not None:
                messagebox.showwarning(
                    "Voucher creati",
                    f"Creati {len(outcome.created)} voucher, ma "
                    "l'aggiornamento dell'elenco non è riuscito. "
                    "Non ripetere la creazione.\n\n"
                    f"{outcome.refresh_error}",
                    parent=self,
                )
                return

            messagebox.showinfo(
                "Voucher",
                f"Creati {len(outcome.created)} voucher. "
                "Sono già selezionati per la stampa.",
                parent=self,
            )

        def failed(exc: Exception) -> None:
            # Ambiguous POST failures are converted by the workflow into a
            # CreateOutcome and therefore deliberately keep the durable marker.
            try:
                self.create_guard.clear()
            except CreateMutationGuardError as guard_exc:
                self.logger.warning(
                    "create_guard_clear_failed type=%s",
                    type(guard_exc).__name__,
                )
            self._show_network_error(
                "Creazione voucher",
                exc,
            )

        started = self._run_network_task(
            "Creazione voucher…",
            worker,
            completed,
            failed,
        )
        if not started:
            try:
                self.create_guard.clear()
            except CreateMutationGuardError:
                pass
