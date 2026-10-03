"""Tk adapters for explicit Voucher Management 5.0 retention review."""

from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox, ttk

from .history import HistoryError
from .ui_layout import fit_toplevel_to_content
from .retention import (
    ensure_retention_policy,
    load_retention_policy,
    mark_retention_intro_seen,
    retention_intro_seen,
    retention_days_configured,
    reviewable_retention_candidates,
    update_retention_days
)


def _display_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        return (
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%d/%m/%Y")
        )
    except ValueError:
        return text


class RetentionIntroDialog(tk.Toplevel):
    """One-time explanation of conservative 5.0 retention defaults."""

    def __init__(self, parent):
        super().__init__(parent)
        self.result: str | None = None
        self.title("Conservazione dati voucher")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Conservazione dati voucher",
            font=("TkDefaultFont", 12, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "Voucher Management conserva lo storico locale per audit e "
                "report. In questa release nessun codice voucher o dato storico "
                "locale viene minimizzato automaticamente. La soglia serve a "
                "individuare record anziani da riesaminare, senza cancellarli."
            ),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(10, 8))
        ttk.Label(
            frame,
            text=(
                "La minimizzazione privacy è una funzione futura separata. "
                "La revoca di sicurezza dalla controller non modifica il codice "
                "voucher né i metadati conservati localmente."
            ),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(0, 14))

        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        ttk.Button(
            actions,
            text="Rivedi conservazione…",
            command=self._review,
        ).pack(side="left")
        ttk.Button(
            actions,
            text="Continua",
            style="Accent.TButton",
            command=self._continue,
        ).pack(side="right")

        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        self.minsize(self.winfo_reqwidth(), self.winfo_reqheight())
        self.wait_window(self)

    def _continue(self) -> None:
        self.result = "continue"
        self.destroy()

    def _review(self) -> None:
        self.result = "review"
        self.destroy()


class RetentionReviewDialog(tk.Toplevel):
    """Review retention policy and explicitly select eligible rows."""

    def __init__(self, app, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.title("Conservazione voucher")
        self.transient(parent or app)
        self.grab_set()

        policy = load_retention_policy(app.database)
        self.days = tk.StringVar(
            value=(
                str(policy.unused_unprinted_days)
                if retention_days_configured(app.database)
                else ""
            )
        )
        self.status = tk.StringVar()

        shell = ttk.Frame(self, padding=18)
        shell.pack(fill="both", expand=True)

        ttk.Label(
            shell,
            text="Conservazione voucher",
            font=("TkDefaultFont", 12, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "L'elenco è solo informativo: mostra voucher anziani non più "
                "presenti sulla controller, mai osservati usati, mai stampati e "
                "senza PDF generati. In questa release nessun dato viene "
                "minimizzato o cancellato da questa schermata."
            ),
            wraplength=820,
            justify="left",
        ).pack(anchor="w", pady=(6, 12))

        policy_row = ttk.Frame(shell)
        policy_row.pack(fill="x", pady=(0, 12))
        ttk.Label(policy_row, text="Età minima scelta").pack(side="left")
        ttk.Spinbox(
            policy_row,
            from_=1,
            to=3650,
            textvariable=self.days,
            width=8,
        ).pack(side="left", padx=(8, 5))
        ttk.Label(policy_row, text="giorni").pack(side="left")
        ttk.Button(
            policy_row,
            text="Aggiorna criteri",
            command=self._save_policy,
        ).pack(side="left", padx=(14, 0))
        ttk.Label(
            policy_row,
            text="Usati: protetti  •  Stampati: protetti",
        ).pack(side="right")

        columns = ("controller", "recipient", "basis", "lastsync")
        self.tree = ttk.Treeview(
            shell,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("controller", text="Controller")
        self.tree.heading("recipient", text="Destinatario")
        self.tree.heading("basis", text="Data di riferimento")
        self.tree.heading("lastsync", text="Ultima sincronizzazione")
        self.tree.column("controller", width=190)
        self.tree.column("recipient", width=260)
        self.tree.column("basis", width=150, anchor="center")
        self.tree.column("lastsync", width=170, anchor="center")

        footer = ttk.Frame(shell)
        footer.pack(side="bottom", fill="x")

        ttk.Label(
            footer,
            textvariable=self.status,
        ).pack(anchor="w", pady=(8, 0))

        actions = ttk.Frame(footer)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(
            actions,
            text="Chiudi",
            command=self.destroy,
        ).pack(side="right")
        ttk.Label(
            actions,
            text="Minimizzazione privacy non attiva in questa release",
            style="Muted.TLabel",
        ).pack(side="left")

        self.tree.pack(fill="both", expand=True)
        self._refresh()
        fit_toplevel_to_content(
            self,
            preferred_width=900,
            preferred_height=560,
            min_width=760,
            min_height=480,
        )

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        try:
            candidates = reviewable_retention_candidates(
                self.app.database,
                history=self.app.history,
                settings=self.app.settings,
                now=self._now(),
                controller_id=None,
            )
        except HistoryError:
            self._candidates = {}
            self.status.set(
                "Candidati non disponibili: cronologia non verificabile."
            )
            messagebox.showerror(
                "Conservazione non disponibile",
                "La cronologia locale non è verificabile. La conservazione "
                "viene bloccata per evitare di minimizzare un voucher che "
                "potrebbe avere un PDF o una stampa registrata.",
                parent=self,
            )
            return
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
                    _display_time(candidate.age_basis),
                    _display_time(candidate.last_synced_at),
                ),
            )
        if not retention_days_configured(self.app.database):
            self.status.set(
                "Scegliere e salvare una soglia prima di calcolare i record da riesaminare."
            )
        else:
            self.status.set(
                f"{len(candidates)} record da riesaminare. Nessuna minimizzazione è attiva."
            )

    def _save_policy(self) -> None:
        try:
            days = int(self.days.get())
            policy = update_retention_days(
                self.app.database,
                days=days,
                now=self._now(),
            )
        except (TypeError, ValueError):
            messagebox.showerror(
                "Conservazione",
                "Inserire un numero di giorni tra 1 e 3650.",
                parent=self,
            )
            return
        self.days.set(str(policy.unused_unprinted_days))
        self._refresh()

class RetentionMixin:
    """Compose retention onboarding and review into the Windows shell."""

    def show_retention_intro_if_needed(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        ensure_retention_policy(self.database, now=now)

        configured = retention_days_configured(self.database)
        intro_seen = retention_intro_seen(self.database)
        if configured and intro_seen:
            return

        if not intro_seen:
            dialog = RetentionIntroDialog(self)
            if dialog.result is None:
                return
            mark_retention_intro_seen(self.database, now=now)

        # Existing/upgraded installations may already have seen the old
        # retention explanation while never having explicitly selected the
        # newly mandatory threshold. Keep prompting the review dialog on
        # startup until a real operator choice is persisted.
        if not retention_days_configured(self.database):
            self.open_retention_review()
            return

    def open_retention_review(self, *, parent=None) -> None:
        ensure_retention_policy(
            self.database,
            now=datetime.now(timezone.utc).isoformat(),
        )
        RetentionReviewDialog(self, parent=parent)
