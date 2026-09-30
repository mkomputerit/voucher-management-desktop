"""Tk adapters for explicit Voucher Management 5.0 retention review."""

from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from uuid import uuid4

from .database import Database
from .history import HistoryError
from .retention import (
    configure_retention_policy,
    load_retention_policy,
    mark_retention_intro_seen,
    retention_intro_seen,
    retention_policy_configured,
    reviewable_retention_candidates,
    security_revocation_candidates,
    prepare_security_revocation_operation,
    archive_retention_candidates,
)
from .sync_store import load_local_vouchers, persist_refresh_snapshot_to_path
from .workflows import delete_vouchers_and_refresh


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
                "Voucher Management separa due azioni: minimizzazione locale "
                "dello storico non più necessario e revoca di sicurezza dei "
                "voucher stampati, ancora attivi su UniFi e mai utilizzati. "
                "Entrambe richiedono una soglia scelta dall'operatore e una "
                "conferma esplicita prima di agire."
            ),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(10, 8))
        ttk.Label(
            frame,
            text=(
                "Il software non imposta automaticamente alcun tempo. "
                "Nessuna revoca o minimizzazione viene eseguita in automatico."
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

        try:
            policy = load_retention_policy(app.database)
        except RuntimeError:
            policy = None
        self.days = tk.StringVar(
            value="" if policy is None else str(policy.unused_unprinted_days)
        )
        self.revoke_days = tk.StringVar(
            value="" if policy is None else str(policy.printed_unused_revoke_days)
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
                "Questa tabella riguarda soltanto la minimizzazione locale. "
                "Mostra voucher non più presenti su UniFi, con utilizzo "
                "osservabile e nessun utilizzo rilevato, abbastanza vecchi "
                "secondo la retention locale. I voucher revocati per sicurezza "
                "possono essere minimizzati in un momento successivo."
            ),
            wraplength=820,
            justify="left",
        ).pack(anchor="w", pady=(6, 12))

        policy_row = ttk.Frame(shell)
        policy_row.pack(fill="x", pady=(0, 12))
        ttk.Label(policy_row, text="Retention locale").pack(side="left")
        ttk.Spinbox(
            policy_row,
            from_=1,
            to=3650,
            textvariable=self.days,
            width=7,
        ).pack(side="left", padx=(6, 4))
        ttk.Label(policy_row, text="gg").pack(side="left")
        ttk.Label(
            policy_row,
            text="Revoca stampati inutilizzati",
        ).pack(side="left", padx=(18, 0))
        ttk.Spinbox(
            policy_row,
            from_=1,
            to=3650,
            textvariable=self.revoke_days,
            width=7,
        ).pack(side="left", padx=(6, 4))
        ttk.Label(policy_row, text="gg").pack(side="left")
        ttk.Button(
            policy_row,
            text="Salva policy",
            command=self._save_policy,
        ).pack(side="left", padx=(14, 0))

        columns = (
            "controller",
            "unifi_description",
            "local_recipient",
            "basis",
            "lastsync",
        )
        self.tree = ttk.Treeview(
            shell,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("controller", text="Controller")
        self.tree.heading("unifi_description", text="Descrizione UniFi")
        self.tree.heading("local_recipient", text="Destinatario locale")
        self.tree.heading("basis", text="Data di riferimento")
        self.tree.heading("lastsync", text="Ultima presenza osservata")
        self.tree.column("controller", width=190)
        self.tree.column("unifi_description", width=240)
        self.tree.column("local_recipient", width=220)
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
        ttk.Button(
            actions,
            text="Revoca sicurezza…",
            command=self._open_security_revocation,
        ).pack(side="left")

        self._refresh()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if not retention_policy_configured(self.app.database):
            self._candidates = {}
            self.status.set(
                "Policy da configurare: inserire entrambe le soglie e salvare."
            )
            return
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
                    candidate.controller_description or "—",
                    candidate.assigned_to or "—",
                    _display_time(candidate.age_basis),
                    _display_time(candidate.last_seen_at),
                ),
            )
        self.status.set(
            f"{len(candidates)} candidati. Nessuna archiviazione è automatica."
        )

    def _save_policy(self) -> None:
        try:
            local_days = int(self.days.get().strip())
            revoke_days = int(self.revoke_days.get().strip())
            policy = configure_retention_policy(
                self.app.database,
                unused_unprinted_days=local_days,
                printed_unused_revoke_days=revoke_days,
                now=self._now(),
            )
        except (TypeError, ValueError):
            messagebox.showerror(
                "Conservazione",
                "Inserire entrambe le soglie con un numero tra 1 e 3650 giorni.",
                parent=self,
            )
            return
        self.days.set(str(policy.unused_unprinted_days))
        self.revoke_days.set(str(policy.printed_unused_revoke_days))
        self._refresh()

    def _open_security_revocation(self) -> None:
        if not retention_policy_configured(self.app.database):
            messagebox.showinfo(
                "Revoca sicurezza",
                "Configurare e salvare entrambe le soglie prima di usare la revoca.",
                parent=self,
            )
            return
        SecurityRevocationDialog(self.app, parent=self)
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


class SecurityRevocationDialog(tk.Toplevel):
    """Operator-reviewed revocation of printed unused credentials from UniFi."""

    def __init__(self, app, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.title("Revoca sicurezza voucher")
        self.transient(parent or app)
        self.grab_set()
        self.geometry("930x540")
        self.minsize(800, 460)
        self.status = tk.StringVar()
        self._candidates = {}

        shell = ttk.Frame(self, padding=18)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text="Voucher da revocare per sicurezza",
            font=("TkDefaultFont", 12, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "Solo voucher stampati, ancora attivi su UniFi, mai utilizzati "
                "secondo evidenza osservata, visti nuovamente dopo la stampa e "
                "oltre la soglia configurata. La revoca elimina la credenziale "
                "dalla controller ma conserva lo storico locale."
            ),
            wraplength=860,
            justify="left",
        ).pack(anchor="w", pady=(6, 12))

        columns = (
            "description",
            "recipient",
            "last_print",
            "last_seen",
            "jobs",
            "copies",
        )
        self.tree = ttk.Treeview(
            shell,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        for key, label in (
            ("description", "Descrizione UniFi"),
            ("recipient", "Destinatario locale"),
            ("last_print", "Ultima stampa"),
            ("last_seen", "Ultima presenza UniFi"),
            ("jobs", "Job stampa"),
            ("copies", "Copie"),
        ):
            self.tree.heading(key, text=label)
        self.tree.column("description", width=230)
        self.tree.column("recipient", width=210)
        self.tree.column("last_print", width=150, anchor="center")
        self.tree.column("last_seen", width=170, anchor="center")
        self.tree.column("jobs", width=90, anchor="center")
        self.tree.column("copies", width=80, anchor="center")
        self.tree.pack(fill="both", expand=True)

        ttk.Label(shell, textvariable=self.status).pack(
            anchor="w", pady=(8, 0)
        )
        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(
            actions,
            text="Chiudi",
            command=self.destroy,
        ).pack(side="right")
        ttk.Button(
            actions,
            text="Revoca selezionati da UniFi…",
            command=self._revoke_selected,
            style="Danger.TButton",
        ).pack(side="right", padx=(0, 8))
        self._refresh()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        controller_id = getattr(self.app, "active_controller_id", None)
        if (
            controller_id is None
            or not getattr(self.app, "client", None)
            or not bool(getattr(self.app, "controller_snapshot_live", False))
        ):
            self._candidates = {}
            self.status.set(
                "Connettersi e sincronizzare con UniFi per verificare i candidati."
            )
            return
        try:
            candidates = security_revocation_candidates(
                self.app.database,
                now=self._now(),
                controller_id=int(controller_id),
            )
        except RuntimeError as exc:
            self._candidates = {}
            self.status.set(str(exc))
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
                    candidate.controller_description or "—",
                    candidate.assigned_to or "—",
                    _display_time(candidate.last_printed_at),
                    _display_time(candidate.last_seen_at),
                    candidate.print_jobs,
                    candidate.physical_copies,
                ),
            )
        self.status.set(
            f"{len(candidates)} candidati. Nessuna revoca è automatica."
        )

    def _revoke_selected(self) -> None:
        selected = [
            int(iid) for iid in self.tree.selection() if iid.isdigit()
        ]
        if not selected:
            messagebox.showinfo(
                "Revoca sicurezza",
                "Selezionare almeno un voucher da revocare.",
                parent=self,
            )
            return

        controller_id = getattr(self.app, "active_controller_id", None)
        client = getattr(self.app, "client", None)
        if controller_id is None or client is None:
            messagebox.showwarning(
                "Revoca sicurezza",
                "La controller non è connessa.",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "Conferma revoca di sicurezza",
            (
                f"Revocare {len(selected)} voucher dalla controller UniFi?\n\n"
                "Le credenziali non saranno più utilizzabili. Lo storico locale "
                "e l'evidenza di stampa resteranno conservati per audit."
            ),
            parent=self,
        ):
            return

        if any(voucher_id not in self._candidates for voucher_id in selected):
            messagebox.showwarning(
                "Revoca sicurezza",
                "L'elenco è cambiato. Aggiornare i candidati e riprovare.",
                parent=self,
            )
            self._refresh()
            return

        database_path = Path(self.app.paths.database)
        operator = self.app._windows_operator_identity()
        operation_uuid = str(uuid4())
        requested_at = self._now()

        def worker():
            fresh = list(client.list_vouchers())
            observed = datetime.now(timezone.utc).isoformat()
            persist_refresh_snapshot_to_path(
                database_path,
                controller_id=int(controller_id),
                vouchers=fresh,
                observed_at=observed,
            )

            db = Database(database_path)
            try:
                db.initialize()
                remote_ids = prepare_security_revocation_operation(
                    db,
                    controller_id=int(controller_id),
                    voucher_ids=selected,
                    operation_uuid=operation_uuid,
                    requested_at=requested_at,
                    windows_user=operator,
                )
            finally:
                db.close()

            by_id = {voucher.id: voucher for voucher in fresh}
            try:
                current = [by_id[remote_id] for remote_id in remote_ids]
            except KeyError as exc:
                raise RuntimeError(
                    "Un voucher candidato non è più presente su UniFi."
                ) from exc

            outcome = delete_vouchers_and_refresh(
                client,
                fresh,
                current,
            )
            if outcome.refresh_error is None:
                persist_refresh_snapshot_to_path(
                    database_path,
                    controller_id=int(controller_id),
                    vouchers=list(outcome.vouchers),
                    observed_at=datetime.now(timezone.utc).isoformat(),
                )
            return outcome

        def completed(outcome) -> None:
            self.app.checked_ids.clear()
            self.app.vouchers = list(outcome.vouchers)
            self.app.controller_snapshot_live = outcome.refresh_error is None
            self.app.populate()
            self._refresh()
            if outcome.refresh_error is not None:
                messagebox.showwarning(
                    "Revoca inviata · verifica richiesta",
                    "La richiesta di revoca è stata inviata a UniFi, ma lo "
                    "snapshot finale non è disponibile. Non ripetere la revoca: "
                    "eseguire Sincronizza per riconciliare lo stato.",
                    parent=self,
                )
                return
            messagebox.showinfo(
                "Revoca sicurezza",
                f"Revocati e verificati: {len(selected)} voucher.",
                parent=self,
            )

        def failed(_exc: Exception) -> None:
            self.app.controller_snapshot_live = False
            self.app.populate()
            messagebox.showwarning(
                "Revoca sicurezza da riconciliare",
                "L'operazione non può essere considerata conclusa. "
                "Sincronizzare con UniFi prima di riprovare: lo stato pendente "
                "verrà riconciliato automaticamente.",
                parent=self,
            )

        self.app._run_network_task(
            "Revoca voucher per sicurezza…",
            worker,
            completed,
            failed,
        )


class RetentionMixin:
    """Compose lifecycle-policy review into the Windows shell."""

    def show_retention_intro_if_needed(self) -> None:
        if not retention_policy_configured(self.database):
            self.after_idle(lambda: RetentionReviewDialog(self))
            return
        if retention_intro_seen(self.database):
            return

        now = datetime.now(timezone.utc).isoformat()
        dialog = RetentionIntroDialog(self)
        if dialog.result is None:
            return

        mark_retention_intro_seen(self.database, now=now)
        if dialog.result == "review":
            self.open_retention_review()

    def open_retention_review(self, *, parent=None) -> None:
        RetentionReviewDialog(self, parent=parent)
