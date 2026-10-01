"""Tk adapter for destructive voucher deletion and recovery."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, simpledialog

from .create_reporting_recovery import reconcile_pending_create_reporting_to_path
from .preparation_deletion import (
    preparation_delete_facts,
    record_preparation_delete_requests_to_path,
)
from .sync_store import persist_refresh_snapshot_to_path
from .unifi_api import UniFiClient
from .workflows import (
    delete_vouchers_and_refresh,
    evaluate_delete_candidates,
    refresh_delete_candidates,
)


class VoucherDeletionMixin:
    """Non-layout deletion workflow for the Windows operator shell."""

    def delete_selected(self) -> None:
        """Revalidate and delete selected vouchers without blocking Tk."""

        if not self.client:
            messagebox.showinfo(
                "UniFi",
                "Connettersi prima al controller UniFi",
                parent=self,
            )
            return

        selected = self.selected()
        if not selected:
            messagebox.showinfo(
                "Elimina da UniFi",
                "Selezionare uno o più voucher attivi.",
                parent=self,
            )
            return

        client = self.client

        def verified(current) -> None:
            self._continue_delete_selected(
                client,
                list(current),
            )

        def verify_failed(exc: Exception) -> None:
            self._show_network_error(
                "Eliminazione",
                exc,
                prefix=(
                    "Impossibile verificare lo stato aggiornato dei voucher. "
                    "Nessun voucher è stato eliminato.\n\n"
                ),
            )

        self._run_network_task(
            "Verifica stato voucher…",
            lambda: refresh_delete_candidates(
                client,
                selected,
            ),
            verified,
            verify_failed,
        )

    def _continue_delete_selected(
        self,
        client: UniFiClient,
        current,
    ) -> None:
        """Apply local policy on Tk, then start the destructive worker."""

        stats = self._history_stats_for(current)
        if stats is None:
            return

        controller_id = getattr(self, "active_controller_id", None)
        paths = getattr(self, "paths", None)
        if (
            controller_id is None
            or getattr(self, "database", None) is None
            or paths is None
        ):
            messagebox.showwarning(
                "Eliminazione non consentita",
                "Lo storico locale non è disponibile. Sincronizzare la controller "
                "prima di riprovare.",
                parent=self,
            )
            return

        remote_ids = [str(voucher.id) for voucher in current]
        historically_used = self.database.historically_used_remote_ids(
            controller_id=controller_id,
            unifi_ids=remote_ids,
        )
        local_facts = preparation_delete_facts(
            self.database,
            controller_id=controller_id,
            unifi_ids=remote_ids,
        )
        if len(local_facts) != len(set(remote_ids)):
            messagebox.showwarning(
                "Eliminazione non consentita",
                "Uno o più voucher non sono presenti nello storico locale. "
                "Eseguire Sincronizza e riprovare.",
                parent=self,
            )
            return

        blocked = evaluate_delete_candidates(
            current,
            stats,
            historically_used_ids=historically_used,
            local_print_states={
                remote_id: fact.print_state
                for remote_id, fact in local_facts.items()
            },
            aligned_ids=frozenset(
                remote_id
                for remote_id, fact in local_facts.items()
                if fact.alignment_completed
            ),
        )
        if blocked:
            reasons = {item.policy.reason for item in blocked}
            if "in_use" in reasons:
                detail = (
                    "Almeno un voucher selezionato risulta già utilizzato o "
                    "in uso sul controller."
                )
            elif "printed" in reasons:
                detail = (
                    "Almeno un voucher selezionato risulta già stampato."
                )
            elif "print_unknown" in reasons:
                detail = (
                    "Per almeno un voucher lo stato di stampa è Non determinabile."
                )
            elif "not_aligned" in reasons:
                detail = (
                    "Almeno un voucher selezionato deve ancora essere allineato."
                )
            else:
                detail = (
                    "Almeno un voucher selezionato non è eliminabile "
                    "dall'applicazione."
                )
            messagebox.showwarning(
                "Eliminazione non consentita",
                f"{detail}\n\nLa cancellazione ordinaria è consentita soltanto "
                "come correzione di preparazione per voucher allineati e "
                "positivamente Non stampati. Gli altri casi devono seguire "
                "il relativo workflow di verifica o sicurezza.",
                parent=self,
            )
            return

        reason = simpledialog.askstring(
            "Motivazione cancellazione",
            (
                "Indicare obbligatoriamente il motivo della correzione di "
                "preparazione. La motivazione resterà nello storico locale."
            ),
            parent=self,
        )
        if reason is None:
            return
        reason = reason.strip()
        if not reason:
            messagebox.showwarning(
                "Motivazione obbligatoria",
                "Inserire una motivazione prima di cancellare il voucher.",
                parent=self,
            )
            return

        if not messagebox.askyesno(
            "Elimina dal server UniFi",
            f"Eliminare {len(current)} voucher dal server UniFi?\n\n"
            f"Motivazione: {reason}\n\n"
            "Questa operazione rimuove i voucher dal controller. Lo storico "
            "locale e la motivazione verranno conservati.",
            parent=self,
        ):
            return

        cached = list(self.vouchers)

        def completed(outcome) -> None:
            self.checked_ids.clear()
            self.vouchers = list(outcome.vouchers)
            self.controller_snapshot_live = outcome.refresh_error is None
            if outcome.refresh_error is not None:
                callback = getattr(self, "_controller_operation_stale", None)
                if callback is not None:
                    callback()
            elif getattr(outcome, "local_persistence_error", None) is not None:
                callback = getattr(self, "_controller_operation_stale", None)
                if callback is not None:
                    callback(archive_failed=True)
            else:
                callback = getattr(self, "_controller_operation_succeeded", None)
                if callback is not None:
                    callback()
            self.populate()

            if outcome.refresh_error is not None:
                messagebox.showwarning(
                    "Eliminazione completata",
                    f"Eliminati {len(current)} voucher, ma l'aggiornamento "
                    "dell'elenco non è riuscito.\n\n"
                    f"{outcome.refresh_error}",
                    parent=self,
                )
                return

            if getattr(outcome, "local_persistence_error", None) is not None:
                self.logger.error(
                    "delete_archive_persistence_failed type=%s",
                    type(getattr(outcome, "local_persistence_error", None)).__name__,
                )
                messagebox.showwarning(
                    "Eliminazione completata • archivio locale da verificare",
                    f"UniFi ha confermato l'eliminazione di {len(current)} "
                    "voucher e l'elenco live è stato aggiornato, ma lo storico "
                    "locale non è stato salvato correttamente. Non ripetere "
                    "l'eliminazione; eseguire Sincronizza per riconciliare.",
                    parent=self,
                )
                return

            messagebox.showinfo(
                "Eliminazione",
                f"Eliminati {len(current)} voucher dal server UniFi.",
                parent=self,
            )

        class _PreparationAuditFailure(RuntimeError):
            pass

        def failed(exc: Exception) -> None:
            if isinstance(exc, _PreparationAuditFailure):
                self.logger.error(
                    "preparation_delete_audit_failed cause=%s",
                    type(exc.__cause__).__name__ if exc.__cause__ is not None else "unknown",
                )
                messagebox.showerror(
                    "Eliminazione non eseguita",
                    "Non è stato possibile salvare la motivazione nello storico. "
                    "Nessun comando DELETE è stato inviato a UniFi.",
                    parent=self,
                )
                return
            self._recover_after_delete_error(
                client,
                exc,
            )

        database_path = Path(paths.database)
        requested_at = datetime.now(timezone.utc).isoformat()
        operator = self._windows_operator_identity()

        def worker():
            try:
                record_preparation_delete_requests_to_path(
                    database_path,
                    controller_id=int(controller_id),
                    unifi_ids=[str(voucher.id) for voucher in current],
                    reason=reason,
                    requested_at=requested_at,
                    windows_user=operator,
                )
            except Exception as exc:
                raise _PreparationAuditFailure(
                    "preparation delete audit failed"
                ) from exc

            outcome = delete_vouchers_and_refresh(
                client,
                cached,
                current,
            )
            if (
                outcome.refresh_error is None
                and controller_id is not None
                and database_path is not None
            ):
                try:
                    persist_refresh_snapshot_to_path(
                        database_path,
                        controller_id=controller_id,
                        vouchers=list(outcome.vouchers),
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    marker_path = getattr(
                        paths,
                        "pending_create_reporting",
                        database_path.with_name("pending_create_reporting.json"),
                    )
                    reconcile_pending_create_reporting_to_path(
                        database_path,
                        marker_path,
                        controller_id=controller_id,
                    )
                except Exception as exc:
                    outcome = replace(
                        outcome,
                        local_persistence_error=exc,
                    )
            return outcome

        self._run_network_task(
            "Eliminazione voucher…",
            worker,
            completed,
            failed,
        )

    def _recover_after_delete_error(
        self,
        client: UniFiClient,
        delete_error: Exception,
    ) -> None:
        """Refresh after a possible partial delete, still outside Tk."""

        controller_id = getattr(self, "active_controller_id", None)
        paths = getattr(self, "paths", None)
        database_path = (
            Path(paths.database)
            if controller_id is not None and paths is not None
            else None
        )

        def worker():
            vouchers = list(client.list_vouchers())
            archive_error = None
            if controller_id is not None and database_path is not None:
                try:
                    persist_refresh_snapshot_to_path(
                        database_path,
                        controller_id=controller_id,
                        vouchers=vouchers,
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    marker_path = getattr(
                        paths,
                        "pending_create_reporting",
                        database_path.with_name("pending_create_reporting.json"),
                    )
                    reconcile_pending_create_reporting_to_path(
                        database_path,
                        marker_path,
                        controller_id=controller_id,
                    )
                except Exception as exc:
                    archive_error = exc
            return vouchers, archive_error

        def refreshed(result) -> None:
            vouchers, archive_error = result
            self.vouchers = list(vouchers)
            self.controller_snapshot_live = True
            self.checked_ids.clear()
            if archive_error is None:
                callback = getattr(self, "_controller_operation_succeeded", None)
                if callback is not None:
                    callback()
            else:
                callback = getattr(self, "_controller_operation_stale", None)
                if callback is not None:
                    callback(archive_failed=True)
            self.populate()
            self._show_network_error(
                "Eliminazione",
                delete_error,
            )

        def refresh_failed(_exc: Exception) -> None:
            self.controller_snapshot_live = False
            callback = getattr(self, "_controller_operation_failed", None)
            if callback is not None:
                callback()
            self.populate()
            self._show_network_error(
                "Eliminazione",
                delete_error,
            )

        self._run_network_task(
            "Aggiornamento dopo errore…",
            worker,
            refreshed,
            refresh_failed,
        )
