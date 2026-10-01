"""Tk UI for created-but-unprinted operational voucher alerts."""

from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox, ttk

from .operational_alerts import (
    set_unprinted_warning_days,
    unprinted_warning_candidates,
    unprinted_warning_days,
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


class OperationalAlertsDialog(tk.Toplevel):
    """Review live vouchers created long ago but positively never printed."""

    def __init__(self, app, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.title("Voucher creati ma non stampati")
        self.transient(parent or app)
        self.grab_set()
        self.geometry("930x560")
        self.minsize(780, 470)

        configured = unprinted_warning_days(app.database)
        self.days = tk.StringVar(
            value="" if configured is None else str(configured)
        )
        self.status = tk.StringVar()

        shell = ttk.Frame(self, padding=18)
        shell.pack(fill="both", expand=True)

        ttk.Label(
            shell,
            text="Voucher creati ma non stampati",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "Evidenzia i voucher ancora presenti sulla controller, mai "
                "osservati utilizzati e positivamente classificati come Non "
                "stampati che hanno superato la soglia dalla data di creazione "
                "UniFi. Questo controllo è solo operativo: non elimina nulla."
            ),
            style="Muted.TLabel",
            wraplength=860,
            justify="left",
        ).pack(anchor="w", pady=(5, 14))

        policy = ttk.Frame(shell)
        policy.pack(fill="x", pady=(0, 12))
        ttk.Label(policy, text="Avvisa dopo").pack(side="left")
        ttk.Spinbox(
            policy,
            from_=1,
            to=3650,
            textvariable=self.days,
            width=8,
        ).pack(side="left", padx=(8, 5))
        ttk.Label(policy, text="giorni dalla creazione").pack(side="left")
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

        columns = ("controller", "recipient", "created", "seen", "synced")
        self.tree = ttk.Treeview(
            shell,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("controller", text="Controller")
        self.tree.heading("recipient", text="Destinatario")
        self.tree.heading("created", text="Creazione UniFi")
        self.tree.heading("seen", text="Ultima presenza")
        self.tree.heading("synced", text="Ultima sync")
        self.tree.column("controller", width=170)
        self.tree.column("recipient", width=250)
        self.tree.column("created", width=150, anchor="center")
        self.tree.column("seen", width=150, anchor="center")
        self.tree.column("synced", width=150, anchor="center")
        self.tree.pack(fill="both", expand=True)

        ttk.Label(
            shell,
            textvariable=self.status,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        ttk.Button(
            shell,
            text="Chiudi",
            command=self.destroy,
        ).pack(anchor="e", pady=(12, 0))

        self._refresh()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _save_threshold(self) -> None:
        try:
            days = int(self.days.get())
            set_unprinted_warning_days(
                self.app.database,
                days=days,
                now=self._now(),
            )
        except (TypeError, ValueError):
            messagebox.showerror(
                "Voucher creati ma non stampati",
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

        if self.app.active_controller_id is None:
            self.status.set(
                "Connettersi a una controller UniFi per verificare i candidati."
            )
            return

        configured = unprinted_warning_days(self.app.database)
        if configured is None:
            self.status.set(
                "Impostare e salvare una soglia prima di cercare candidati."
            )
            return

        candidates = unprinted_warning_candidates(
            self.app.database,
            now=self._now(),
            controller_id=self.app.active_controller_id,
        )
        for candidate in candidates:
            self.tree.insert(
                "",
                "end",
                iid=str(candidate.voucher_id),
                values=(
                    candidate.controller_name,
                    candidate.recipient or "—",
                    _display_time(candidate.created_at),
                    _display_time(candidate.last_seen_at),
                    _display_time(candidate.last_synced_at),
                ),
            )

        self.status.set(
            f"{len(candidates)} voucher oltre la soglia di {configured} giorni. "
            "Nessuna cancellazione è automatica."
        )


class OperationalAlertsMixin:
    """Expose created-but-unprinted review from the Windows shell."""

    def open_operational_alerts(self, *, parent=None) -> None:
        OperationalAlertsDialog(self, parent=parent)
