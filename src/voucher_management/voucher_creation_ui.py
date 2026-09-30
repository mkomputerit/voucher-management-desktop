"""Tk adapter for voucher creation and uncertain-mutation protection."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox

from .create_reporting_recovery import (
    CreateReportingRecoveryError,
    clear_pending_create_reporting,
    load_pending_create_reporting,
    write_pending_create_reporting,
)
from .dialogs import CreateDialog
from .mutation_guard import CreateMutationGuardError
from .sync_store import persist_create_result_to_path
from .workflows import CreateOutcome, create_vouchers_and_refresh


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
            if bool(
                getattr(
                    self.create_guard,
                    "requires_manual_recovery",
                    False,
                )
            ):
                detail = (
                    "Una precedente creazione è stata confermata da UniFi, "
                    "ma la classificazione locale non è stata conservata in "
                    "modo recuperabile. Per proteggere la reportistica, nuove "
                    "creazioni restano bloccate finché l'archivio locale non "
                    "viene verificato/riparato manualmente."
                )
            else:
                detail = (
                    "Una precedente creazione ha un esito da verificare. "
                    "Per evitare voucher duplicati, eseguire prima Aggiorna e "
                    "controllare l'elenco restituito dal controller."
                )
            messagebox.showwarning(
                "Creazione sospesa",
                detail,
                parent=self,
            )
            return

        reporting_marker = getattr(
            getattr(self, "paths", None),
            "pending_create_reporting",
            None,
        )
        if reporting_marker is not None:
            try:
                pending_reporting = load_pending_create_reporting(reporting_marker)
            except CreateReportingRecoveryError:
                pending_reporting = True
            if pending_reporting:
                messagebox.showwarning(
                    "Creazione sospesa",
                    "Esiste una creazione già confermata da UniFi la cui "
                    "classificazione locale deve ancora essere riconciliata. "
                    "Eseguire prima Sincronizza/Aggiorna.",
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
        controller_id = getattr(self, "active_controller_id", None)
        paths = getattr(self, "paths", None)
        database_path = (
            Path(paths.database)
            if controller_id is not None and paths is not None
            else None
        )

        def worker():
            outcome = create_vouchers_and_refresh(
                client,
                cached,
                params,
            )
            if (
                controller_id is None
                or database_path is None
                or not outcome.created
                or outcome.uncertain_error is not None
            ):
                return outcome

            observed_at = datetime.now(timezone.utc).isoformat()
            marker_path = getattr(
                paths,
                "pending_create_reporting",
                database_path.with_name("pending_create_reporting.json"),
            )
            marker_error = None
            try:
                write_pending_create_reporting(
                    marker_path,
                    controller_id=controller_id,
                    voucher_ids=[voucher.id for voucher in outcome.created],
                    is_nominal=bool(params.get("is_nominal", False)),
                    confirmed_at=observed_at,
                )
            except Exception as exc:
                marker_error = exc
                # The reporting marker is the preferred durable recovery path.
                # If it cannot be written, escalate the pre-existing mutation
                # guard *before* SQLite persistence so a hard crash cannot
                # reopen creation with un-attributable confirmed vouchers.
                try:
                    self.create_guard.mark_confirmed_unreconciled()
                except CreateMutationGuardError as guard_exc:
                    self.logger.error(
                        "create_guard_manual_recovery_mark_failed type=%s",
                        type(guard_exc).__name__,
                    )

            try:
                persist_create_result_to_path(
                    database_path,
                    controller_id=controller_id,
                    snapshot=list(outcome.vouchers),
                    created=list(outcome.created),
                    snapshot_complete=outcome.snapshot_complete,
                    snapshot_observed=outcome.refresh_error is None,
                    is_nominal=bool(params.get("is_nominal", False)),
                    observed_at=observed_at,
                )
            except Exception as exc:
                # The controller result is already confirmed. Preserve the
                # durable reconciliation marker when available and never
                # recast this as a failed/uncertain POST.
                return replace(
                    outcome,
                    local_persistence_error=exc,
                    recovery_marker_error=marker_error,
                )

            # SQLite now contains the same confirmed UUID/classification facts
            # atomically. A leftover reporting marker is redundant and may be
            # cleared. If that marker had failed earlier, SQLite persistence is
            # now the durable recovery fact, so release the escalated mutation
            # guard here instead of waiting for a UI callback.
            try:
                clear_pending_create_reporting(marker_path)
            except OSError:
                # Reconciliation is idempotent; leaving the marker behind is
                # safer than converting a confirmed create into a failure.
                pass
            if marker_error is not None:
                try:
                    self.create_guard.clear()
                except CreateMutationGuardError:
                    # The completion callback retries and warns the operator.
                    pass
            return replace(outcome, recovery_marker_error=None)

        def completed(outcome) -> None:
            self.vouchers = list(outcome.vouchers)
            self.controller_snapshot_live = bool(
                outcome.refresh_error is None
                and bool(getattr(outcome, "snapshot_complete", True))
                and not bool(getattr(outcome, "reconciliation_required", False))
            )
            if getattr(outcome, "local_persistence_error", None) is not None:
                callback = getattr(self, "_controller_operation_stale", None)
                if callback is not None:
                    callback(archive_failed=True)
            elif self.controller_snapshot_live:
                callback = getattr(self, "_controller_operation_succeeded", None)
                if callback is not None:
                    callback()
            else:
                callback = getattr(self, "_controller_operation_stale", None)
                if callback is not None:
                    callback()

            if outcome.uncertain_error is not None:
                self.checked_ids.clear()
                self.populate()
                if outcome.refresh_error is None:
                    detail = (
                        "L'elenco è stato riletto dal controller, ma in una "
                        "configurazione multi-postazione non è sicuro attribuire "
                        "automaticamente eventuali nuovi voucher a questa richiesta."
                    )
                    if getattr(outcome, "local_persistence_error", None) is not None:
                        detail += (
                            "\nInoltre l'archivio locale dei report non è stato "
                            "aggiornato correttamente."
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

            local_persistence_failed = (
                getattr(outcome, "local_persistence_error", None) is not None
            )
            reporting_marker_failed = (
                getattr(outcome, "recovery_marker_error", None) is not None
            )
            manual_recovery_required = (
                local_persistence_failed and reporting_marker_failed
            )

            if manual_recovery_required:
                try:
                    self.create_guard.mark_confirmed_unreconciled()
                except CreateMutationGuardError as exc:
                    # The original guard still exists. Keep the in-memory
                    # fail-closed flag set by mark_confirmed_unreconciled() and
                    # never clear it from this completion path.
                    self.logger.error(
                        "create_guard_manual_recovery_mark_failed type=%s",
                        type(exc).__name__,
                    )
            else:
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

            if getattr(outcome, "local_persistence_error", None) is not None:
                self.logger.error(
                    "create_reporting_persistence_failed type=%s",
                    type(getattr(outcome, "local_persistence_error", None)).__name__,
                )
                marker_ok = getattr(
                    outcome,
                    "recovery_marker_error",
                    None,
                ) is None
                recovery_detail = (
                    "La riconciliazione è stata salvata e verrà riprovata "
                    "al prossimo aggiornamento riuscito."
                    if marker_ok
                    else (
                        "Non è stato possibile salvare neppure il marker di "
                        "riconciliazione: non creare altri voucher e verificare "
                        "l'archivio locale prima di proseguire."
                    )
                )
                messagebox.showwarning(
                    "Voucher creati • archivio locale da verificare",
                    f"UniFi ha confermato la creazione di {len(outcome.created)} "
                    "voucher, ma la loro classificazione nello storico locale "
                    "non è stata registrata correttamente.\n\n"
                    "Non ripetere la creazione. "
                    f"{recovery_detail}",
                    parent=self,
                )
                return

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

            if bool(getattr(outcome, "reconciliation_required", False)):
                messagebox.showwarning(
                    "Voucher creati • elenco da aggiornare",
                    f"UniFi ha confermato la creazione di {len(outcome.created)} "
                    "voucher, ma la prima rilettura non li conteneva ancora "
                    "tutti. I voucher confermati restano visibili e selezionati. "
                    "Eseguire Sincronizza prima di considerare la Home aggiornata.",
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
