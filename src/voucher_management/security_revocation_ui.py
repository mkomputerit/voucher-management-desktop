"""Tk UI for explicit printed-unused voucher security revocation."""

from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox, ttk

from .database import Database
from .security_revocation import (
    pending_security_revocation_ids,
    revoke_security_candidates_live,
    security_revocation_candidates,
    security_revoke_days,
    set_security_revoke_days,
)


def _display_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        return (
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%d/%m/%Y %H:%M")
        )
    except ValueError:
        return text


class SecurityRevocationDialog(tk.Toplevel):
    """Review printed-unused vouchers and explicitly revoke them from UniFi."""

    def __init__(self, app, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.title("Revoca di sicurezza")
        self.transient(parent or app)
        self.grab_set()
        self.geometry("980x590")
        self.minsize(820, 500)

        configured = security_revoke_days(app.database)
        self.days = tk.StringVar(
            value="" if configured is None else str(configured)
        )
        self.status = tk.StringVar()
        self._candidates = {}

        shell = ttk.Frame(self, padding=18)
        shell.pack(fill="both", expand=True)

        ttk.Label(
            shell,
            text="Revoca di sicurezza voucher inutilizzati",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "Individua voucher già stampati che non risultano mai osservati "
                "come utilizzati e che hanno superato la soglia scelta. Se una "
                "stampa storica è certa ma la sua data non è determinabile, il "
                "voucher viene mostrato subito perché non è possibile calcolarne "
                "l'età di sicurezza. Prima di ogni eliminazione il voucher viene "
                "riletto direttamente dalla controller UniFi. Il codice e lo "
                "storico locale restano conservati integralmente dopo la revoca."
            ),
            style="Muted.TLabel",
            wraplength=900,
            justify="left",
        ).pack(anchor="w", pady=(5, 14))

        policy = ttk.Frame(shell)
        policy.pack(fill="x", pady=(0, 12))
        ttk.Label(policy, text="Revoca dopo").pack(side="left")
        ttk.Spinbox(
            policy,
            from_=1,
            to=3650,
            textvariable=self.days,
            width=8,
        ).pack(side="left", padx=(8, 5))
        ttk.Label(policy, text="giorni dalla stampa").pack(side="left")
        ttk.Button(
            policy,
            text="Salva soglia",
            command=self._save_threshold,
        ).pack(side="left", padx=(14, 0))
        ttk.Button(
            policy,
            text="Aggiorna elenco",
            command=self._refresh,
        ).pack(side="right")

        columns = ("controller", "recipient", "printed", "seen", "synced")
        self.tree = ttk.Treeview(
            shell,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("controller", text="Controller")
        self.tree.heading("recipient", text="Destinatario")
        self.tree.heading("printed", text="Ultima stampa")
        self.tree.heading("seen", text="Ultima presenza")
        self.tree.heading("synced", text="Ultima sync")
        self.tree.column("controller", width=170)
        self.tree.column("recipient", width=250)
        self.tree.column("printed", width=150, anchor="center")
        self.tree.column("seen", width=150, anchor="center")
        self.tree.column("synced", width=150, anchor="center")
        self.tree.pack(fill="both", expand=True)

        ttk.Label(
            shell,
            textvariable=self.status,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(
            actions,
            text="Chiudi",
            command=self.destroy,
        ).pack(side="right")
        self.revoke_button = ttk.Button(
            actions,
            text="Revoca selezionati…",
            style="Accent.TButton",
            command=self._revoke_selected,
        )
        self.revoke_button.pack(side="right", padx=(0, 8))

        self._refresh()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _save_threshold(self) -> None:
        try:
            days = int(self.days.get())
            set_security_revoke_days(
                self.app.database,
                days=days,
                now=self._now(),
            )
        except (TypeError, ValueError):
            messagebox.showerror(
                "Revoca di sicurezza",
                "Inserire un numero di giorni tra 1 e 3650.",
                parent=self,
            )
            return
        self.days.set(str(days))
        self._refresh()
        refresh_home = getattr(self.app, "_refresh_home_threshold_alerts", None)
        if refresh_home is not None:
            refresh_home()
        refresh_settings = getattr(self.app, "_refresh_threshold_summary", None)
        if refresh_settings is not None:
            refresh_settings()

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._candidates = {}

        if self.app.active_controller_id is None:
            self.status.set("Connettersi a una controller UniFi per verificare i candidati.")
            self.revoke_button.state(["disabled"])
            return

        configured = security_revoke_days(self.app.database)
        if configured is None:
            self.status.set(
                "Impostare e salvare una soglia prima di cercare candidati."
            )
            self.revoke_button.state(["disabled"])
            return

        candidates = security_revocation_candidates(
            self.app.database,
            now=self._now(),
            controller_id=self.app.active_controller_id,
        )
        self._candidates = {
            candidate.voucher_id: candidate for candidate in candidates
        }
        for candidate in candidates:
            self.tree.insert(
                "",
                "end",
                iid=str(candidate.voucher_id),
                values=(
                    candidate.controller_name,
                    candidate.recipient or "—",
                    (
                        _display_time(candidate.last_printed_at)
                        if candidate.print_date_known
                        else "Non determinabile"
                    ),
                    _display_time(candidate.last_seen_at),
                    _display_time(candidate.last_synced_at),
                ),
            )

        pending = pending_security_revocation_ids(
            self.app.database,
            controller_id=self.app.active_controller_id,
        )
        suffix = (
            f" • {len(pending)} revoche da riconciliare"
            if pending else ""
        )
        unknown_print_date = sum(
            not candidate.print_date_known
            for candidate in candidates
        )
        unknown_suffix = (
            f" • {unknown_print_date} con data stampa non determinabile"
            if unknown_print_date
            else ""
        )
        self.status.set(
            f"{len(candidates)} candidati alla verifica live{suffix}"
            f"{unknown_suffix}. Nessuna revoca è automatica."
        )
        if candidates and self.app.client is not None:
            self.revoke_button.state(["!disabled"])
        else:
            self.revoke_button.state(["disabled"])

    def _revoke_selected(self) -> None:
        if self.app.client is None:
            messagebox.showinfo(
                "Revoca di sicurezza",
                "Connettersi prima alla controller UniFi.",
                parent=self,
            )
            return

        selected = [
            self._candidates[int(iid)]
            for iid in self.tree.selection()
            if iid.isdigit() and int(iid) in self._candidates
        ]
        if not selected:
            messagebox.showinfo(
                "Revoca di sicurezza",
                "Selezionare almeno un voucher candidato.",
                parent=self,
            )
            return

        if not messagebox.askyesno(
            "Conferma revoca",
            (
                f"Revocare {len(selected)} voucher dalla controller UniFi?\n\n"
                "Ogni voucher verrà riletto subito prima della DELETE. Se nel "
                "frattempo risulta usato o scaduto, verrà ignorato. Il codice "
                "voucher e lo storico locale NON verranno cancellati."
            ),
            parent=self,
        ):
            return

        client = self.app.client
        operator = self.app._windows_operator_identity()
        database_path = self.app.paths.database
        revoked_at = self._now()

        def worker():
            database = Database(database_path)
            try:
                database.initialize()
                return revoke_security_candidates_live(
                    database,
                    client=client,
                    candidates=selected,
                    revoked_at=revoked_at,
                    windows_user=operator,
                )
            finally:
                database.close()

        def completed(result) -> None:
            self.app.checked_ids.clear()
            self._refresh()
            refresh_home = getattr(self.app, "_refresh_home_threshold_alerts", None)
            if refresh_home is not None:
                refresh_home()
            try:
                self.app.refresh()
            except Exception:
                # The revocation audit is already durable. A refresh failure is
                # handled by the normal controller state machine on next Sync.
                self.app.populate()

            details = [
                f"Revocati: {len(result.revoked_ids)}",
                f"Ignorati perché non più idonei: {len(result.skipped_ids)}",
                f"Operazioni non confermate: {len(result.failed_ids)}",
                (
                    "Revocati da UniFi ma da riconciliare localmente: "
                    f"{len(result.local_persistence_failed_ids)}"
                ),
            ]
            messagebox.showinfo(
                "Revoca di sicurezza",
                "\n".join(details),
                parent=self,
            )

        def failed(exc: Exception) -> None:
            self.app.logger.warning(
                "security_revocation_failed type=%s",
                type(exc).__name__,
            )
            self._refresh()
            messagebox.showerror(
                "Revoca di sicurezza",
                "Impossibile completare la revoca. Verificare lo stato della "
                "controller e sincronizzare prima di riprovare.",
                parent=self,
            )

        self.app._run_background_task(
            "Revoca voucher inutilizzati…",
            worker,
            completed,
            failed,
        )


class SecurityRevocationMixin:
    """Expose security-revocation review from the Windows shell."""

    def open_security_revocation(self, *, parent=None) -> None:
        SecurityRevocationDialog(self, parent=parent)
