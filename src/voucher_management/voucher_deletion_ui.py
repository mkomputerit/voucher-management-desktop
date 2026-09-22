"""Tk adapter for destructive voucher deletion and recovery."""

from __future__ import annotations

from tkinter import messagebox

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

        blocked = evaluate_delete_candidates(
            current,
            stats,
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
            else:
                detail = (
                    "Almeno un voucher selezionato non è eliminabile "
                    "dall'applicazione."
                )
            messagebox.showwarning(
                "Eliminazione non consentita",
                f"{detail}\n\nVoucher Management consente solo la pulizia "
                "dei voucher non ancora emessi. L'eventuale revoca resta di "
                "competenza dell'amministratore IT.",
                parent=self,
            )
            return

        if not messagebox.askyesno(
            "Elimina dal server UniFi",
            f"Eliminare {len(current)} voucher dal server UniFi?\n\n"
            "Questa operazione rimuove i voucher dal controller. Lo storico "
            "locale e gli eventuali PDF già generati non verranno cancellati.",
            parent=self,
        ):
            return

        cached = list(self.vouchers)

        def completed(outcome) -> None:
            self.checked_ids.clear()
            self.vouchers = list(outcome.vouchers)
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

            messagebox.showinfo(
                "Eliminazione",
                f"Eliminati {len(current)} voucher dal server UniFi.",
                parent=self,
            )

        def failed(exc: Exception) -> None:
            self._recover_after_delete_error(
                client,
                exc,
            )

        self._run_network_task(
            "Eliminazione voucher…",
            lambda: delete_vouchers_and_refresh(
                client,
                cached,
                current,
            ),
            completed,
            failed,
        )

    def _recover_after_delete_error(
        self,
        client: UniFiClient,
        delete_error: Exception,
    ) -> None:
        """Refresh after a possible partial delete, still outside Tk."""

        def refreshed(vouchers) -> None:
            self.vouchers = list(vouchers)
            self.checked_ids.clear()
            self.populate()
            self._show_network_error(
                "Eliminazione",
                delete_error,
            )

        def refresh_failed(_exc: Exception) -> None:
            self._show_network_error(
                "Eliminazione",
                delete_error,
            )

        self._run_network_task(
            "Aggiornamento dopo errore…",
            client.list_vouchers,
            refreshed,
            refresh_failed,
        )
