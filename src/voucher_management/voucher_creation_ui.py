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
from .uncertain_create_recovery import (
    UncertainCreateRecoveryError,
    clear_pending_create_intent,
    confirm_pending_create_intent_to_path,
    load_pending_create_intent,
    match_pending_create_intent,
    reject_pending_create_intent_to_path,
    write_pending_create_intent,
)
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
        if not bool(getattr(self, "controller_snapshot_live", False)):
            messagebox.showwarning(
                "Creazione sospesa",
                "Prima di creare voucher serve una fotografia UniFi live e "
                "completa. Eseguire Sincronizza e riprovare.",
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
        if controller_id is None or database_path is None or paths is None:
            messagebox.showwarning(
                "Creazione sospesa",
                "La controller è collegata ma il profilo locale non è pronto "
                "per registrare in sicurezza una creazione incerta. "
                "Eseguire Sincronizza e riprovare.",
                parent=self,
            )
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

        intent_path = getattr(
            paths,
            "pending_create_intent",
            database_path.with_name("pending_create_intent.json"),
        )
        requested_at = datetime.now(timezone.utc).isoformat()
        try:
            site_id = str(
                self.database.controller_site_id(int(controller_id))
                or getattr(client, "site_id", "")
                or ""
            ).strip()
            recipient_digest = self.history.correlation_digest(
                "uncertain-create-recipient",
                str(params.get("recipient") or ""),
                self.settings,
            )
            down_mbps = params.get("down_mbps")
            up_mbps = params.get("up_mbps")
            write_pending_create_intent(
                intent_path,
                controller_id=int(controller_id),
                site_id=site_id,
                requested_at=requested_at,
                baseline_ids=[
                    str(voucher.id)
                    for voucher in cached
                    if str(getattr(voucher, "id", "") or "").strip()
                ],
                quantity=int(params["quantity"]),
                recipient_digest=recipient_digest,
                duration_minutes=(
                    int(params["expire_number"])
                    * int(params["expire_unit"])
                ),
                quota=int(params["quota"]),
                data_mb=(
                    None
                    if params.get("data_mb") is None
                    else int(params["data_mb"])
                ),
                down_kbps=(
                    None
                    if down_mbps is None
                    else int(down_mbps) * 1000
                ),
                up_kbps=(
                    None
                    if up_mbps is None
                    else int(up_mbps) * 1000
                ),
                is_nominal=bool(params.get("is_nominal", False)),
            )
        except Exception as exc:
            try:
                self.create_guard.clear()
            except CreateMutationGuardError:
                pass
            self.logger.warning(
                "uncertain_create_intent_write_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Creazione sospesa",
                "Non è stato possibile registrare il riferimento di sicurezza "
                "necessario per riconoscere una creazione interrotta. "
                "Nessuna richiesta è stata inviata a UniFi.",
                parent=self,
            )
            return

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
                if marker_error is None:
                    try:
                        clear_pending_create_intent(intent_path)
                    except OSError:
                        pass
                return replace(
                    outcome,
                    local_persistence_error=exc,
                    recovery_marker_error=marker_error,
                )

            # SQLite now contains the same confirmed UUID/classification facts
            # atomically. A leftover marker is redundant and may be cleared.
            try:
                clear_pending_create_reporting(marker_path)
            except OSError:
                # Reconciliation is idempotent; leaving the marker behind is
                # safer than converting a confirmed create into a failure.
                pass
            try:
                clear_pending_create_intent(intent_path)
            except OSError:
                # Confirmed UUID state is already durable in SQLite.
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
                        "L'elenco è stato riletto dal controller. Eventuali "
                        "voucher compatibili non verranno attribuiti "
                        "automaticamente alla richiesta: al prossimo "
                        "Sincronizza verrà chiesta una conferma esplicita "
                        "all'operatore."
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

            self.filter_var.set("Da stampare")
            ui_refreshed = self._finalize_voucher_operation_ui(
                operation="create",
            )

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
                    "tutti. I voucher confermati restano visibili ma non "
                    "selezionati. Eseguire Sincronizza prima di considerare "
                    "completa una nuova fotografia UniFi.",
                    parent=self,
                )
                return

            if not ui_refreshed:
                return
            messagebox.showinfo(
                "Voucher",
                f"Creati {len(outcome.created)} voucher. "
                "Home e Voucher sono stati aggiornati automaticamente.",
                parent=self,
            )

        def failed(exc: Exception) -> None:
            # Ambiguous POST failures are converted by the workflow into a
            # CreateOutcome and therefore deliberately keep both durable markers.
            try:
                clear_pending_create_intent(intent_path)
            except OSError:
                pass
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
                clear_pending_create_intent(intent_path)
            except OSError:
                pass
            try:
                self.create_guard.clear()
            except CreateMutationGuardError as exc:
                self.logger.warning(
                    "create_guard_clear_after_task_reject_failed type=%s",
                    type(exc).__name__,
                )
                messagebox.showwarning(
                    "Creazione sospesa",
                    "La richiesta di creazione non è partita, ma non è stato "
                    "possibile rimuovere il blocco di sicurezza locale. "
                    "Chiudere e riaprire l'applicazione o usare la procedura "
                    "di recupero prima di tentare una nuova creazione.",
                    parent=self,
                )


    def _offer_uncertain_create_recovery_after_refresh(
        self,
        vouchers,
    ) -> bool:
        """Resolve an uncertain POST only through an explicit operator decision.

        True means a durable uncertain-create intent exists, so the generic
        refresh path must not clear the anti-repeat guard on its own.
        """

        paths = getattr(self, "paths", None)
        controller_id = getattr(self, "active_controller_id", None)
        if paths is None or controller_id is None:
            return False
        intent_path = getattr(
            paths,
            "pending_create_intent",
            Path(paths.database).with_name("pending_create_intent.json"),
        )
        try:
            pending = load_pending_create_intent(intent_path)
        except UncertainCreateRecoveryError as exc:
            self.logger.warning(
                "uncertain_create_intent_invalid type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Creazione da verificare",
                "Il riferimento della creazione interrotta non è leggibile. "
                "La creazione resta bloccata per sicurezza.",
                parent=self,
            )
            return True
        if pending is None:
            return False

        site_id = str(
            self.database.controller_site_id(int(controller_id)) or ""
        ).strip()
        if (
            pending.controller_id != int(controller_id)
            or not site_id
            or pending.site_id != site_id
        ):
            messagebox.showerror(
                "Creazione da verificare",
                "La richiesta interrotta appartiene a un'altra identità "
                "controller/Site. Nessuna associazione è stata eseguita e "
                "la creazione resta bloccata.",
                parent=self,
            )
            return True

        try:
            match = match_pending_create_intent(
                pending,
                list(vouchers),
                recipient_digest=lambda value: self.history.correlation_digest(
                    "uncertain-create-recipient",
                    value,
                    self.settings,
                ),
            )
        except Exception as exc:
            self.logger.warning(
                "uncertain_create_match_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Creazione da verificare",
                "Non è stato possibile confrontare in modo sicuro i voucher "
                "con la richiesta interrotta. La creazione resta bloccata.",
                parent=self,
            )
            return True

        candidate_ids = list(match.compatible_ids)
        if match.exact:
            decision = messagebox.askyesnocancel(
                "Creazione interrotta rilevata",
                (
                    f"Sono stati trovati {len(candidate_ids)} voucher nuovi "
                    "compatibili con la richiesta interrotta.\n\n"
                    "Confermare che questi voucher sono stati creati da quella "
                    "richiesta?\n\n"
                    "Sì = associa i voucher alla richiesta.\n"
                    "No = lasciali come voucher esterni e chiudi la richiesta.\n"
                    "Annulla = rimanda la decisione e mantieni il blocco."
                ),
                parent=self,
            )
            if decision is None:
                return True
            associate = bool(decision)
        else:
            close_without_association = messagebox.askyesno(
                "Creazione interrotta non riconciliata",
                (
                    "La sincronizzazione è riuscita, ma non è stato trovato un "
                    "insieme univoco di voucher compatibile con la richiesta "
                    f"interrotta ({len(candidate_ids)} compatibili su "
                    f"{pending.quantity} attesi).\n\n"
                    "Dopo aver verificato la controller, vuoi chiudere la "
                    "richiesta come NON associata e sbloccare nuove creazioni?\n\n"
                    "Sì = chiudi la richiesta senza attribuire voucher.\n"
                    "No = mantieni il blocco e riprova con una sincronizzazione "
                    "successiva."
                ),
                parent=self,
            )
            if not close_without_association:
                return True
            associate = False

        database_path = Path(paths.database)
        decided_at = datetime.now(timezone.utc).isoformat()
        operator = self._windows_operator_identity()

        def worker():
            if associate:
                return (
                    "associated",
                    confirm_pending_create_intent_to_path(
                        database_path,
                        intent_path,
                        controller_id=int(controller_id),
                        candidate_ids=candidate_ids,
                        confirmed_at=decided_at,
                        windows_user=operator,
                    ),
                )
            return (
                "rejected",
                reject_pending_create_intent_to_path(
                    database_path,
                    intent_path,
                    controller_id=int(controller_id),
                    candidate_ids=candidate_ids,
                    rejected_at=decided_at,
                    windows_user=operator,
                ),
            )

        def completed(result) -> None:
            action, _local_ids = result
            try:
                self.create_guard.clear()
            except CreateMutationGuardError as exc:
                self.logger.warning(
                    "create_guard_clear_after_recovery_failed type=%s",
                    type(exc).__name__,
                )
                messagebox.showwarning(
                    "Creazione riconciliata",
                    "La decisione è stata registrata, ma il blocco "
                    "anti-ripetizione non può essere rimosso automaticamente. "
                    "Riavviare l'applicazione prima di creare altri voucher.",
                    parent=self,
                )
                return

            if action == "associated":
                self.checked_ids = set(candidate_ids)
                filter_var = getattr(self, "filter_var", None)
                if filter_var is not None:
                    filter_var.set("Da stampare")
                messagebox.showinfo(
                    "Creazione riconciliata",
                    f"Associati {len(candidate_ids)} voucher alla richiesta "
                    "interrotta. Sono ora trattati come creati da Voucher "
                    "Management.",
                    parent=self,
                )
            else:
                messagebox.showinfo(
                    "Creazione chiusa",
                    "La richiesta interrotta è stata chiusa senza associare "
                    "voucher. Gli eventuali voucher trovati restano di origine "
                    "controller e seguiranno il normale allineamento.",
                    parent=self,
                )
            self.populate()
            refresh = getattr(self, "_refresh_report_summary", None)
            if callable(refresh):
                refresh()

        def failed(exc: Exception) -> None:
            self.logger.warning(
                "uncertain_create_recovery_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Creazione da verificare",
                "Non è stato possibile registrare la decisione. "
                "La creazione resta bloccata e nessuna associazione è stata "
                "applicata.",
                parent=self,
            )

        started = self._run_background_task(
            "Riconcilia creazione interrotta…",
            worker,
            completed,
            failed,
        )
        if not started:
            messagebox.showwarning(
                "Creazione da verificare",
                "Un'altra operazione è in corso. La decisione non è stata "
                "registrata e il blocco resta attivo.",
                parent=self,
            )
        return True

