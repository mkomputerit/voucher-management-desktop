"""Focused Tk workflows for Voucher Management-owned voucher data.

The Voucher workspace keeps the original operational selection.  Local actions
never introduce a second recipient field and never modify UniFi-owned fields.
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

from .alignment import (
    PRINT_STATE_NOT_PRINTED,
    PRINT_STATE_PRINTED,
    PRINT_STATE_UNKNOWN,
    align_vouchers_to_path,
    alignment_candidates,
)
from .local_data import LocalVoucherPatch, apply_local_voucher_patch_to_path


_NOMINAL_VALUES = {
    "Nominale": True,
    "Non nominale": False,
}

_PRINT_VALUES = {
    "Stampato": PRINT_STATE_PRINTED,
    "Non stampato": PRINT_STATE_NOT_PRINTED,
    "Non determinabile": PRINT_STATE_UNKNOWN,
}


def selected_workspace_vouchers(app) -> tuple:
    """Return exactly the vouchers selected in the existing Voucher table."""

    selected = set(getattr(app, "checked_ids", set()))
    return tuple(
        voucher
        for voucher in getattr(app, "vouchers", ())
        if str(getattr(voucher, "id", "")) in selected
        or getattr(voucher, "id", None) in selected
    )


def _local_ids(app, vouchers) -> tuple[int, ...]:
    controller_id = getattr(app, "active_controller_id", None)
    if controller_id is None:
        raise RuntimeError("Nessuna controller attiva.")
    ids: list[int] = []
    for voucher in vouchers:
        row = app.database.connection.execute(
            """SELECT id FROM vouchers
               WHERE controller_id=? AND unifi_id=? AND archived_at IS NULL""",
            (int(controller_id), str(voucher.id)),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "Uno dei voucher selezionati non è presente nello storico locale. "
                "Eseguire Sincronizza e riprovare."
            )
        ids.append(int(row["id"]))
    return tuple(ids)


class NominalityDialog(tk.Toplevel):
    def __init__(self, app, vouchers):
        super().__init__(app)
        self.app = app
        self.vouchers = tuple(vouchers)
        self.value = tk.StringVar(value="")
        self.status = tk.StringVar()
        self.title("Nominalità voucher")
        self.transient(app)
        self.grab_set()
        self.resizable(False, False)

        shell = ttk.Frame(self, padding=20)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text="Imposta nominalità",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                f"La scelta verrà applicata a {len(self.vouchers)} voucher "
                "selezionati. Il destinatario e gli altri dati UniFi non "
                "verranno modificati."
            ),
            style="Muted.TLabel",
            wraplength=520,
        ).pack(anchor="w", pady=(5, 14))
        ttk.Combobox(
            shell,
            textvariable=self.value,
            values=tuple(_NOMINAL_VALUES),
            state="readonly",
            width=24,
        ).pack(anchor="w")
        ttk.Label(shell, textvariable=self.status, style="Muted.TLabel").pack(
            anchor="w", pady=(10, 0)
        )

        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(18, 0))
        ttk.Button(actions, text="Annulla", command=self.destroy).pack(side="right")
        self.save_button = ttk.Button(
            actions,
            text="Applica",
            style="Accent.TButton",
            command=self._save,
        )
        self.save_button.pack(side="right", padx=(0, 8))

    def _save(self) -> None:
        if self.value.get() not in _NOMINAL_VALUES:
            messagebox.showinfo(
                "Nominalità",
                "Selezionare Nominale oppure Non nominale.",
                parent=self,
            )
            return
        try:
            voucher_ids = _local_ids(self.app, self.vouchers)
        except RuntimeError as exc:
            messagebox.showerror("Nominalità", str(exc), parent=self)
            return

        patch = LocalVoucherPatch(
            apply_is_nominal=True,
            is_nominal=_NOMINAL_VALUES[self.value.get()],
        )
        controller_id = int(self.app.active_controller_id)
        database_path = Path(self.app.paths.database)
        updated_at = datetime.now(timezone.utc).isoformat()
        operator = self.app._windows_operator_identity()
        self.save_button.state(["disabled"])
        self.status.set("Salvataggio nominalità…")

        def worker():
            return apply_local_voucher_patch_to_path(
                database_path,
                controller_id=controller_id,
                voucher_ids=voucher_ids,
                patch=patch,
                updated_at=updated_at,
                windows_user=operator,
            )

        def completed(result) -> None:
            refresh = getattr(self.app, "_refresh_report_summary", None)
            if callable(refresh):
                refresh()
            self.destroy()
            messagebox.showinfo(
                "Nominalità",
                (
                    f"Aggiornati: {len(result.updated_ids)}. "
                    f"Già coerenti: {len(result.unchanged_ids)}."
                ),
                parent=self.app,
            )

        def failed(exc: Exception) -> None:
            self.save_button.state(["!disabled"])
            self.status.set("")
            self.app.logger.warning(
                "voucher_nominality_batch_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Nominalità",
                "Salvataggio non riuscito. Nessun aggiornamento parziale è stato applicato.",
                parent=self,
            )

        self.app._run_background_task(
            "Salvataggio nominalità…", worker, completed, failed
        )


class NotesDialog(tk.Toplevel):
    def __init__(self, app, vouchers):
        super().__init__(app)
        self.app = app
        self.vouchers = tuple(vouchers)
        self.notes = tk.StringVar()
        self.status = tk.StringVar()
        self.title("Note voucher")
        self.transient(app)
        self.grab_set()
        self.resizable(False, False)
        self._prefill()

        shell = ttk.Frame(self, padding=20)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="Note locali", style="SectionTitle.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            shell,
            text=(
                f"La nota verrà applicata a {len(self.vouchers)} voucher "
                "selezionati. Lasciare il campo vuoto per cancellare la nota locale."
            ),
            style="Muted.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(5, 12))
        ttk.Entry(shell, textvariable=self.notes, width=72).pack(fill="x")
        ttk.Label(shell, textvariable=self.status, style="Muted.TLabel").pack(
            anchor="w", pady=(10, 0)
        )

        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(18, 0))
        ttk.Button(actions, text="Annulla", command=self.destroy).pack(side="right")
        self.save_button = ttk.Button(
            actions,
            text="Salva",
            style="Accent.TButton",
            command=self._save,
        )
        self.save_button.pack(side="right", padx=(0, 8))

    def _prefill(self) -> None:
        try:
            ids = _local_ids(self.app, self.vouchers)
        except RuntimeError:
            return
        placeholders = ",".join("?" for _ in ids)
        rows = self.app.database.connection.execute(
            f"SELECT notes FROM vouchers WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        values = {str(row["notes"] or "") for row in rows}
        if len(values) == 1:
            self.notes.set(next(iter(values)))

    def _save(self) -> None:
        try:
            voucher_ids = _local_ids(self.app, self.vouchers)
        except RuntimeError as exc:
            messagebox.showerror("Note voucher", str(exc), parent=self)
            return

        patch = LocalVoucherPatch(apply_notes=True, notes=self.notes.get())
        controller_id = int(self.app.active_controller_id)
        database_path = Path(self.app.paths.database)
        updated_at = datetime.now(timezone.utc).isoformat()
        operator = self.app._windows_operator_identity()
        self.save_button.state(["disabled"])
        self.status.set("Salvataggio note…")

        def worker():
            return apply_local_voucher_patch_to_path(
                database_path,
                controller_id=controller_id,
                voucher_ids=voucher_ids,
                patch=patch,
                updated_at=updated_at,
                windows_user=operator,
            )

        def completed(result) -> None:
            refresh = getattr(self.app, "_refresh_report_summary", None)
            if callable(refresh):
                refresh()
            self.destroy()
            messagebox.showinfo(
                "Note voucher",
                (
                    f"Aggiornati: {len(result.updated_ids)}. "
                    f"Già coerenti: {len(result.unchanged_ids)}."
                ),
                parent=self.app,
            )

        def failed(exc: Exception) -> None:
            self.save_button.state(["!disabled"])
            self.status.set("")
            self.app.logger.warning(
                "voucher_notes_batch_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Note voucher",
                "Salvataggio non riuscito. Nessun aggiornamento parziale è stato applicato.",
                parent=self,
            )

        self.app._run_background_task("Salvataggio note…", worker, completed, failed)


class AlignmentDialog(tk.Toplevel):
    """Align vouchers discovered on UniFi without inventing missing history."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.nominal = tk.StringVar(value="")
        self.print_state = tk.StringVar(value="")
        self.status = tk.StringVar()
        self._candidates = {}
        self.title("Allinea voucher")
        self.transient(app)
        self.grab_set()
        self.geometry("850x540")
        self.minsize(720, 460)

        shell = ttk.Frame(self, padding=18)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text="Voucher da allineare",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "Sono voucher trovati sulla controller per cui il nuovo database "
                "non possiede ancora tutte le informazioni locali. Seleziona uno "
                "o più voucher e indica la nominalità. Per i voucher creati fuori "
                "da Voucher Management la stampa resta 'Non determinabile' salvo "
                "evidenza verificata nello storico."
            ),
            style="Muted.TLabel",
            wraplength=790,
        ).pack(anchor="w", pady=(5, 12))

        self.tree = ttk.Treeview(
            shell,
            columns=("name", "created", "print"),
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("name", text="Destinatario")
        self.tree.heading("created", text="Creazione")
        self.tree.heading("print", text="Stampa nota")
        self.tree.column("name", width=350)
        self.tree.column("created", width=180, anchor="center")
        self.tree.column("print", width=180, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._sync_alignment_fields)

        fields = ttk.Frame(shell)
        fields.pack(fill="x", pady=(12, 0))
        ttk.Label(fields, text="Nominalità").grid(row=0, column=0, sticky="w")
        self.nominal_combo = ttk.Combobox(
            fields,
            textvariable=self.nominal,
            values=tuple(_NOMINAL_VALUES),
            state="readonly",
            width=20,
        )
        self.nominal_combo.grid(row=0, column=1, sticky="w", padx=(8, 20))
        ttk.Label(fields, text="Stato stampa").grid(row=0, column=2, sticky="w")
        self.print_combo = ttk.Combobox(
            fields,
            textvariable=self.print_state,
            values=tuple(_PRINT_VALUES),
            state="readonly",
            width=22,
        )
        self.print_combo.grid(row=0, column=3, sticky="w", padx=(8, 0))

        ttk.Label(shell, textvariable=self.status, style="Muted.TLabel").pack(
            anchor="w", pady=(10, 0)
        )
        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="Chiudi", command=self.destroy).pack(side="right")
        self.save_button = ttk.Button(
            actions,
            text="Allinea selezionati",
            style="Accent.TButton",
            command=self._save,
        )
        self.save_button.pack(side="right", padx=(0, 8))
        self._refresh()

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        controller_id = getattr(self.app, "active_controller_id", None)
        if controller_id is None:
            self._candidates = {}
            self.status.set("Nessuna controller attiva.")
            return
        candidates = alignment_candidates(
            self.app.database,
            controller_id=int(controller_id),
            present_only=True,
        )
        self._candidates = {str(item.voucher_id): item for item in candidates}
        for item in candidates:
            if item.last_printed_at:
                print_label = f"Verificata {item.last_printed_at[:10]}"
            elif str(item.print_state or "").strip().upper() == PRINT_STATE_PRINTED:
                print_label = "Stampato • data non determinabile"
            elif str(item.origin or "").strip().upper() == "CONTROLLER":
                print_label = "Non determinabile"
            else:
                print_label = "Da dichiarare"
            self.tree.insert(
                "",
                "end",
                iid=str(item.voucher_id),
                values=(
                    item.name or "—",
                    item.created_at[:10] if item.created_at else "—",
                    print_label,
                ),
            )
        self.status.set(
            f"{len(candidates)} voucher da allineare."
            if candidates
            else "Nessun voucher da allineare."
        )
        self.save_button.state(["!disabled"] if candidates else ["disabled"])
        self.nominal.set("")
        self.print_state.set("")
        self.nominal_combo.configure(state="readonly")
        self.print_combo.configure(state="readonly")

    def _sync_alignment_fields(self, _event=None) -> None:
        selected = [
            self._candidates[value]
            for value in self.tree.selection()
            if value in self._candidates
        ]
        if not selected:
            self.nominal.set("")
            self.print_state.set("")
            self.nominal_combo.configure(state="readonly")
            self.print_combo.configure(state="readonly")
            return

        known_nominal = {item.is_nominal for item in selected if item.is_nominal is not None}
        unknown_nominal = any(item.is_nominal is None for item in selected)
        if not unknown_nominal and len(known_nominal) == 1:
            value = next(iter(known_nominal))
            self.nominal.set("Nominale" if value else "Non nominale")
            self.nominal_combo.configure(state="disabled")
        else:
            self.nominal.set("")
            self.nominal_combo.configure(state="readonly")

        positive_print = [
            bool(item.last_printed_at)
            or str(item.print_state or "").strip().upper()
            == PRINT_STATE_PRINTED
            for item in selected
        ]
        origins = {
            str(item.origin or "").strip().upper()
            for item in selected
        }
        if positive_print and all(positive_print):
            self.print_state.set("Stampato")
            self.print_combo.configure(state="disabled")
        elif origins == {"CONTROLLER"}:
            self.print_state.set("Non determinabile")
            self.print_combo.configure(state="disabled")
        else:
            self.print_state.set("")
            self.print_combo.configure(state="readonly")

    def _save(self) -> None:
        selected = tuple(self.tree.selection())
        if not selected:
            messagebox.showinfo(
                "Allinea voucher",
                "Selezionare almeno un voucher.",
                parent=self,
            )
            return

        if self.nominal.get() not in _NOMINAL_VALUES:
            messagebox.showinfo(
                "Allinea voucher",
                "Selezionare Nominale oppure Non nominale.",
                parent=self,
            )
            return
        if self.print_state.get() not in _PRINT_VALUES:
            messagebox.showinfo(
                "Allinea voucher",
                "Selezionare lo stato stampa.",
                parent=self,
            )
            return

        selected_candidates = [self._candidates[value] for value in selected]
        positive_print_flags = [
            bool(item.last_printed_at)
            or str(item.print_state or "").strip().upper()
            == PRINT_STATE_PRINTED
            for item in selected_candidates
        ]
        if any(positive_print_flags) and not all(positive_print_flags):
            messagebox.showinfo(
                "Allinea voucher",
                "La selezione mescola voucher con prova positiva di stampa e "
                "voucher senza tale evidenza. Allinearli in due gruppi separati.",
                parent=self,
            )
            return

        known_nominal = {
            item.is_nominal
            for item in selected_candidates
            if item.is_nominal is not None
        }
        has_unknown_nominal = any(
            item.is_nominal is None for item in selected_candidates
        )
        if (
            (not has_unknown_nominal and len(known_nominal) > 1)
            or (has_unknown_nominal and bool(known_nominal))
        ):
            messagebox.showinfo(
                "Allinea voucher",
                "La selezione mescola voucher con nominalità già assegnata e "
                "voucher ancora da classificare, oppure contiene classificazioni "
                "diverse. Allineare i gruppi separatamente.",
                parent=self,
            )
            return

        requested_print = _PRINT_VALUES[self.print_state.get()]
        if (
            requested_print != PRINT_STATE_PRINTED
            and any(positive_print_flags)
        ):
            messagebox.showerror(
                "Allinea voucher",
                "La selezione contiene una prova positiva di stampa nello storico. "
                "Per questi voucher lo stato deve rimanere Stampato.",
                parent=self,
            )
            return

        controller_id = int(self.app.active_controller_id)
        database_path = Path(self.app.paths.database)
        aligned_at = datetime.now(timezone.utc).isoformat()
        operator = self.app._windows_operator_identity()
        self.save_button.state(["disabled"])
        self.status.set("Allineamento in corso…")

        def worker():
            return align_vouchers_to_path(
                database_path,
                controller_id=controller_id,
                voucher_ids=[int(value) for value in selected],
                is_nominal=_NOMINAL_VALUES[self.nominal.get()],
                print_state=requested_print,
                aligned_at=aligned_at,
                windows_user=operator,
            )

        def completed(result) -> None:
            refresh = getattr(self.app, "_refresh_report_summary", None)
            if callable(refresh):
                refresh()
            self._refresh()
            messagebox.showinfo(
                "Allinea voucher",
                (
                    f"Allineati: {len(result.updated_ids)}. "
                    f"Già coerenti: {len(result.unchanged_ids)}."
                ),
                parent=self,
            )

        def failed(exc: Exception) -> None:
            self.save_button.state(["!disabled"])
            self.status.set("")
            self.app.logger.warning(
                "voucher_alignment_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Allinea voucher",
                str(exc),
                parent=self,
            )

        self.app._run_background_task("Allineamento voucher…", worker, completed, failed)


class LocalDataMixin:
    """Explicit local actions exposed by the Voucher workspace."""

    def _require_workspace_selection(self, title: str) -> tuple:
        vouchers = selected_workspace_vouchers(self)
        if not vouchers:
            messagebox.showinfo(
                title,
                "Selezionare almeno un voucher nell'elenco.",
                parent=self,
            )
        return vouchers

    def edit_selected_nominality(self) -> None:
        vouchers = self._require_workspace_selection("Nominalità")
        if vouchers:
            NominalityDialog(self, vouchers)

    def edit_selected_notes(self) -> None:
        vouchers = self._require_workspace_selection("Note voucher")
        if not vouchers:
            return
        if len(vouchers) != 1:
            messagebox.showinfo(
                "Note voucher",
                "Le note locali possono essere modificate su un solo voucher alla volta.",
                parent=self,
            )
            return
        NotesDialog(self, vouchers)

    def align_pending_vouchers(self) -> None:
        if getattr(self, "active_controller_id", None) is None:
            messagebox.showinfo(
                "Allinea voucher",
                "Connettersi e sincronizzare la controller prima di allineare i voucher.",
                parent=self,
            )
            return
        AlignmentDialog(self)
