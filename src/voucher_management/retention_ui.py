"""Tk adapters for explicit Voucher Management 5.0 retention review."""

from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox, ttk

from .history import HistoryError
from .retention import (
    ensure_retention_policy,
    load_retention_policy,
    mark_retention_intro_seen,
    retention_intro_seen,
    reviewable_retention_candidates,
    update_retention_days,
    archive_retention_candidates,
)
from .sync_store import load_local_vouchers


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
                "report. I voucher utilizzati o stampati sono sempre protetti. "
                "Solo voucher mai usati, mai stampati, non più presenti sul "
                "controller, senza PDF generati e abbastanza vecchi possono "
                "essere proposti per la minimizzazione."
            ),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(10, 8))
        ttk.Label(
            frame,
            text=(
                "La soglia consigliata è 180 giorni. Nessun voucher viene "
                "archiviato automaticamente: la pulizia richiede sempre una "
                "revisione e una conferma esplicita."
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
        self.geometry("900x560")
        self.minsize(760, 480)

        policy = load_retention_policy(app.database)
        self.days = tk.StringVar(value=str(policy.unused_unprinted_days))
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
                "Le protezioni per voucher utilizzati e stampati sono "
                "obbligatorie e non possono essere disattivate. L'elenco "
                "sottostante contiene soltanto voucher non più presenti sul "
                "controller, mai usati, mai stampati e senza PDF generati."
            ),
            wraplength=820,
            justify="left",
        ).pack(anchor="w", pady=(6, 12))

        policy_row = ttk.Frame(shell)
        policy_row.pack(fill="x", pady=(0, 12))
        ttk.Label(policy_row, text="Età minima").pack(side="left")
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
        self.tree.pack(fill="both", expand=True)

        ttk.Label(
            shell,
            textvariable=self.status,
        ).pack(anchor="w", pady=(8, 0))

        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(
            actions,
            text="Chiudi",
            command=self.destroy,
        ).pack(side="right")
        ttk.Button(
            actions,
            text="Archivia selezionati…",
            command=self._archive_selected,
        ).pack(side="right", padx=(0, 8))

        self._refresh()

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
        self.status.set(
            f"{len(candidates)} candidati. Nessuna archiviazione è automatica."
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

    def _archive_selected(self) -> None:
        selected = [
            int(iid)
            for iid in self.tree.selection()
            if iid.isdigit()
        ]
        if not selected:
            messagebox.showinfo(
                "Conservazione",
                "Selezionare almeno un candidato da archiviare.",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "Conferma archiviazione",
            (
                f"Archiviare {len(selected)} voucher selezionati?\n\n"
                "Il record storico resterà disponibile, ma codice voucher, "
                "destinatario, assegnazione e note verranno rimossi. "
                "L'operazione non viene eseguita sui voucher che nel frattempo "
                "non soddisfano più i criteri."
            ),
            parent=self,
        ):
            return

        try:
            result = archive_retention_candidates(
                self.app.database,
                voucher_ids=selected,
                archived_at=self._now(),
                windows_user=self.app._windows_operator_identity(),
                history=self.app.history,
                settings=self.app.settings,
            )
        except HistoryError:
            messagebox.showerror(
                "Conservazione non disponibile",
                "La cronologia locale non è verificabile. Nessun voucher è "
                "stato archiviato.",
                parent=self,
            )
            return
        except Exception as exc:
            self.app.logger.warning(
                "retention_archive_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Conservazione",
                "Impossibile completare l'archiviazione selezionata. Nessun "
                "voucher è stato minimizzato parzialmente.",
                parent=self,
            )
            return
        if self.app.active_controller_id is not None:
            # Archivable rows are already absent from a live controller snapshot,
            # so a connected Home does not need to replace its fresh list with
            # the broader historical SQLite cache. In local/offline mode, reload
            # that cache but keep Home explicitly non-live.
            if not bool(getattr(self.app, "controller_snapshot_live", False)):
                self.app.vouchers = load_local_vouchers(
                    self.app.database,
                    controller_id=self.app.active_controller_id,
                )
                self.app.controller_snapshot_live = False
            self.app.checked_ids.clear()
            self.app.populate()

        self._refresh()
        messagebox.showinfo(
            "Conservazione",
            (
                f"Archiviati: {len(result.archived_ids)}. "
                f"Non più idonei e quindi ignorati: {len(result.skipped_ids)}."
            ),
            parent=self,
        )


class RetentionMixin:
    """Compose retention onboarding and review into the Windows shell."""

    def show_retention_intro_if_needed(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        ensure_retention_policy(self.database, now=now)
        if retention_intro_seen(self.database):
            return

        dialog = RetentionIntroDialog(self)
        if dialog.result is None:
            return

        mark_retention_intro_seen(self.database, now=now)
        if dialog.result == "review":
            self.open_retention_review()

    def open_retention_review(self, *, parent=None) -> None:
        ensure_retention_policy(
            self.database,
            now=datetime.now(timezone.utc).isoformat(),
        )
        RetentionReviewDialog(self, parent=parent)
