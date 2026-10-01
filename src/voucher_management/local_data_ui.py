"""Tk UI for editing Voucher Management-owned voucher metadata only."""

from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

from .local_data import (
    LocalVoucherPatch,
    apply_local_voucher_patch_to_path,
)


_NOMINAL_VALUES = {
    "Nominale": True,
    "Non nominale": False,
    "Non classificato": None,
}


def local_data_selection_candidates(vouchers) -> tuple:
    """Return controller rows eligible for local-only metadata editing.

    Local metadata is independent from print/delete lifecycle policy, therefore
    expired vouchers remain selectable here even though the operational table
    deliberately blocks them from destructive/printing selection.
    """

    return tuple(vouchers)


class LocalDataSelectionDialog(tk.Toplevel):
    """Independent selector for local metadata, including expired vouchers."""

    def __init__(self, app, vouchers, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.vouchers = local_data_selection_candidates(vouchers)
        self.result = None
        self.title("Seleziona voucher per Dati locali")
        self.transient(parent or app)
        self.grab_set()
        self.geometry("820x500")
        self.minsize(700, 420)

        shell = ttk.Frame(self, padding=18)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text="Seleziona voucher per Dati locali",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "Questa selezione è separata da stampa ed eliminazione. "
                "Sono inclusi anche i voucher scaduti perché nominalità, "
                "destinatario e note locali servono alla reportistica storica."
            ),
            style="Muted.TLabel",
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(5, 12))

        columns = ("voucher", "description", "status")
        self.tree = ttk.Treeview(
            shell,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("voucher", text="Voucher")
        self.tree.heading("description", text="Descrizione UniFi")
        self.tree.heading("status", text="Stato")
        self.tree.column("voucher", width=180)
        self.tree.column("description", width=380)
        self.tree.column("status", width=140, anchor="center")
        self.tree.pack(fill="both", expand=True)

        preselected = set(getattr(app, "checked_ids", set()))
        selected_iids = []
        for voucher in self.vouchers:
            iid = str(voucher.id)
            status = (
                "Scaduto"
                if str(getattr(voucher, "status", "") or "") == "EXPIRED"
                else "Attivo"
            )
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    str(getattr(voucher, "code_formatted", "") or ""),
                    str(getattr(voucher, "recipient", "") or "") or "—",
                    status,
                ),
            )
            if voucher.id in preselected:
                selected_iids.append(iid)
        if selected_iids:
            self.tree.selection_set(selected_iids)

        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(
            actions,
            text="Annulla",
            command=self.destroy,
        ).pack(side="right")
        ttk.Button(
            actions,
            text="Continua",
            style="Accent.TButton",
            command=self._accept,
        ).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    def _accept(self) -> None:
        selected = set(self.tree.selection())
        if not selected:
            messagebox.showinfo(
                "Dati locali",
                "Selezionare almeno un voucher.",
                parent=self,
            )
            return
        self.result = tuple(
            voucher
            for voucher in self.vouchers
            if str(voucher.id) in selected
        )
        self.destroy()


class LocalDataDialog(tk.Toplevel):
    """Batch editor that never writes controller-owned voucher fields."""

    def __init__(self, app, vouchers, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.vouchers = tuple(vouchers)
        self.title("Dati locali")
        self.transient(parent or app)
        self.grab_set()
        self.resizable(False, False)

        self.apply_assigned = tk.BooleanVar(value=False)
        self.assigned_to = tk.StringVar()
        self.apply_nominal = tk.BooleanVar(value=False)
        self.nominal = tk.StringVar(value="Non classificato")
        self.apply_notes = tk.BooleanVar(value=False)
        self.notes = tk.StringVar()
        self.status = tk.StringVar()
        self._prefill_common_values()

        shell = ttk.Frame(self, padding=20)
        shell.pack(fill="both", expand=True)

        ttk.Label(
            shell,
            text="Dati locali dei voucher selezionati",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                f"{len(self.vouchers)} voucher selezionati. "
                "Questa schermata modifica solo dati conservati localmente da "
                "Voucher Management. Codice, descrizione UniFi, durata, "
                "scadenza e contatori della controller restano in sola lettura."
            ),
            style="Muted.TLabel",
            wraplength=650,
            justify="left",
        ).pack(anchor="w", pady=(5, 16))

        card = ttk.Frame(shell)
        card.pack(fill="x")
        card.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            card,
            text="Applica destinatario locale",
            variable=self.apply_assigned,
            command=self._sync_state,
        ).grid(row=0, column=0, sticky="w", padx=(0, 12), pady=6)
        self.assigned_entry = ttk.Entry(
            card,
            textvariable=self.assigned_to,
            width=44,
        )
        self.assigned_entry.grid(row=0, column=1, sticky="ew", pady=6)

        ttk.Checkbutton(
            card,
            text="Applica Voucher nominale",
            variable=self.apply_nominal,
            command=self._sync_state,
        ).grid(row=1, column=0, sticky="w", padx=(0, 12), pady=6)
        self.nominal_combo = ttk.Combobox(
            card,
            textvariable=self.nominal,
            values=tuple(_NOMINAL_VALUES),
            state="readonly",
            width=24,
        )
        self.nominal_combo.grid(row=1, column=1, sticky="w", pady=6)

        ttk.Checkbutton(
            card,
            text="Applica note locali",
            variable=self.apply_notes,
            command=self._sync_state,
        ).grid(row=2, column=0, sticky="w", padx=(0, 12), pady=6)
        self.notes_entry = ttk.Entry(
            card,
            textvariable=self.notes,
            width=60,
        )
        self.notes_entry.grid(row=2, column=1, sticky="ew", pady=6)

        ttk.Label(
            shell,
            text=(
                "Lasciare vuoto un campo selezionato significa cancellare il "
                "relativo valore locale. “Non classificato” rimuove soltanto "
                "la classificazione nominale locale."
            ),
            style="Muted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(12, 4))

        ttk.Label(
            shell,
            textvariable=self.status,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(18, 0))
        ttk.Button(
            actions,
            text="Annulla",
            command=self.destroy,
        ).pack(side="right")
        self.save_button = ttk.Button(
            actions,
            text="Salva dati locali",
            style="Accent.TButton",
            command=self._save,
        )
        self.save_button.pack(side="right", padx=(0, 8))

        self._sync_state()
        self.update_idletasks()
        width = min(max(720, self.winfo_reqwidth() + 24), self.winfo_screenwidth() - 80)
        height = min(max(360, self.winfo_reqheight() + 24), self.winfo_screenheight() - 100)
        self.geometry(f"{width}x{height}")

    def _prefill_common_values(self) -> None:
        """Show common local values without implicitly applying them."""

        controller_id = getattr(self.app, "active_controller_id", None)
        if controller_id is None or not self.vouchers:
            return
        remote_ids = tuple(
            dict.fromkeys(str(voucher.id).strip() for voucher in self.vouchers)
        )
        if not remote_ids:
            return
        placeholders = ",".join("?" for _ in remote_ids)
        rows = self.app.database.connection.execute(
            f"""SELECT assigned_to, notes, is_nominal
                FROM vouchers
                WHERE controller_id=?
                  AND unifi_id IN ({placeholders})
                  AND archived_at IS NULL""",
            (int(controller_id), *remote_ids),
        ).fetchall()
        if len(rows) != len(remote_ids):
            return

        assigned_values = {str(row["assigned_to"] or "") for row in rows}
        notes_values = {str(row["notes"] or "") for row in rows}
        nominal_values = {
            None if row["is_nominal"] is None else bool(row["is_nominal"])
            for row in rows
        }
        if len(assigned_values) == 1:
            self.assigned_to.set(next(iter(assigned_values)))
        if len(notes_values) == 1:
            self.notes.set(next(iter(notes_values)))
        if len(nominal_values) == 1:
            common = next(iter(nominal_values))
            for label, value in _NOMINAL_VALUES.items():
                if value is common:
                    self.nominal.set(label)
                    break

    def _sync_state(self) -> None:
        self.assigned_entry.state(
            ["!disabled"] if self.apply_assigned.get() else ["disabled"]
        )
        self.nominal_combo.configure(
            state="readonly" if self.apply_nominal.get() else "disabled"
        )
        self.notes_entry.state(
            ["!disabled"] if self.apply_notes.get() else ["disabled"]
        )

    def _save(self) -> None:
        controller_id = getattr(self.app, "active_controller_id", None)
        if controller_id is None:
            messagebox.showerror(
                "Dati locali",
                "Nessuna controller locale associata ai voucher selezionati.",
                parent=self,
            )
            return

        patch = LocalVoucherPatch(
            apply_assigned_to=bool(self.apply_assigned.get()),
            assigned_to=self.assigned_to.get(),
            apply_notes=bool(self.apply_notes.get()),
            notes=self.notes.get(),
            apply_is_nominal=bool(self.apply_nominal.get()),
            is_nominal=_NOMINAL_VALUES[self.nominal.get()],
        )
        try:
            patch = patch.validated()
        except ValueError as exc:
            messagebox.showerror("Dati locali", str(exc), parent=self)
            return

        voucher_ids = []
        for voucher in self.vouchers:
            local_id = self.app.database.connection.execute(
                """SELECT id FROM vouchers
                   WHERE controller_id=? AND unifi_id=?""",
                (int(controller_id), str(voucher.id)),
            ).fetchone()
            if local_id is None:
                messagebox.showerror(
                    "Dati locali",
                    "Uno dei voucher selezionati non è presente nello storico "
                    "locale. Eseguire Sincronizza e riprovare.",
                    parent=self,
                )
                return
            voucher_ids.append(int(local_id["id"]))

        operator = self.app._windows_operator_identity()
        database_path = Path(self.app.paths.database)
        updated_at = datetime.now(timezone.utc).isoformat()
        self.save_button.state(["disabled"])
        self.status.set("Salvataggio dati locali…")

        def worker():
            return apply_local_voucher_patch_to_path(
                database_path,
                controller_id=int(controller_id),
                voucher_ids=voucher_ids,
                patch=patch,
                updated_at=updated_at,
                windows_user=operator,
            )

        def completed(result) -> None:
            self.app.checked_ids.clear()
            self.app._sync_selection_ui()
            refresh_reports = getattr(self.app, "_refresh_report_summary", None)
            if callable(refresh_reports):
                refresh_reports()
            self.destroy()
            messagebox.showinfo(
                "Dati locali",
                (
                    f"Aggiornati: {len(result.updated_ids)}. "
                    f"Già coerenti: {len(result.unchanged_ids)}.\n\n"
                    "Nessun dato della controller UniFi è stato modificato."
                ),
                parent=self.app,
            )

        def failed(exc: Exception) -> None:
            self.save_button.state(["!disabled"])
            self.status.set("")
            self.app.logger.warning(
                "local_data_batch_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Dati locali",
                "Salvataggio non riuscito. Nessun aggiornamento parziale è "
                "stato applicato; la selezione è rimasta invariata.",
                parent=self,
            )

        self.app._run_background_task(
            "Salvataggio dati locali…",
            worker,
            completed,
            failed,
        )


class LocalDataMixin:
    """Expose local metadata batch editing from the voucher workspace."""

    def edit_selected_local_data(self) -> None:
        candidates = local_data_selection_candidates(
            getattr(self, "vouchers", ())
        )
        if not candidates:
            messagebox.showinfo(
                "Dati locali",
                "Non ci sono voucher disponibili per la controller corrente.",
                parent=self,
            )
            return

        selector = LocalDataSelectionDialog(
            self,
            candidates,
            parent=self,
        )
        if not selector.result:
            return
        LocalDataDialog(self, selector.result, parent=self)
