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
from .ui_layout import fit_toplevel_to_content


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
    """Return the current blue selection in the Voucher workspace.

    This is the single UI selection used by local metadata actions. Printing
    and deletion apply their own stricter eligibility checks at the action
    boundary, so selecting a row never grants permission to mutate it.
    """

    selected = set(getattr(app, "checked_ids", set()))
    return tuple(
        voucher
        for voucher in getattr(app, "vouchers", ())
        if str(getattr(voucher, "id", "")) in selected
        or getattr(voucher, "id", None) in selected
    )


def local_data_selection_candidates(vouchers) -> tuple:
    """Return rows eligible for local-only metadata correction.

    Nominality and notes are reporting metadata, not print/delete authority.
    Therefore expired vouchers remain selectable here even though operational
    print/delete selection deliberately excludes them.
    """

    return tuple(vouchers)


def voucher_action_states(app) -> dict[str, bool]:
    """Return fail-closed availability for the Voucher action menu.

    The blue workspace selection is the single source of truth. Local metadata
    remains editable when permitted by its own backend rules; alignment is
    enabled only when every selected voucher is currently an alignment
    candidate, so mixed selections cannot produce a partial alignment.
    """

    selected = selected_workspace_vouchers(app)
    states = {
        "align": False,
        "nominality": False,
        "notes": False,
    }
    if not selected:
        return states

    try:
        _local_ids(app, selected)
    except (RuntimeError, ValueError, TypeError):
        local_metadata_ready = False
    else:
        local_metadata_ready = True

    states["nominality"] = local_metadata_ready
    states["notes"] = local_metadata_ready and len(selected) == 1

    if any(
        str(getattr(voucher, "status", "") or "").strip().upper() == "EXPIRED"
        for voucher in selected
    ):
        return states

    controller_id = getattr(app, "active_controller_id", None)
    database = getattr(app, "database", None)
    if controller_id is None or database is None:
        return states

    try:
        pending = alignment_candidates(
            database,
            controller_id=int(controller_id),
            present_only=True,
        )
    except Exception:
        return states

    selected_remote_ids = {
        str(getattr(voucher, "id", "") or "").strip()
        for voucher in selected
    }
    selected_remote_ids.discard("")
    pending_remote_ids = {
        str(getattr(item, "unifi_id", "") or "").strip()
        for item in pending
    }
    states["align"] = bool(selected_remote_ids) and (
        selected_remote_ids <= pending_remote_ids
    )
    return states


def _local_ids(app, vouchers) -> tuple[int, ...]:
    """Resolve live UniFi UUIDs to local SQLite voucher primary keys.

    ApiVoucher.id is the UniFi voucher UUID, while local metadata mutations use
    the integer primary key of the vouchers table. Resolution is scoped to the
    active controller and fails closed if any selected voucher is missing,
    archived, ambiguous or has an empty remote identity.
    """

    controller_id = getattr(app, "active_controller_id", None)
    database = getattr(app, "database", None)
    if controller_id is None or database is None:
        raise RuntimeError(
            "La controller locale attiva non è disponibile. Sincronizzare e riprovare."
        )

    remote_ids = tuple(
        dict.fromkeys(
            str(getattr(voucher, "id", "") or "").strip()
            for voucher in vouchers
        )
    )
    if not remote_ids or any(not value for value in remote_ids):
        raise RuntimeError(
            "Uno o più voucher selezionati non hanno un'identità UniFi valida."
        )

    placeholders = ",".join("?" for _ in remote_ids)
    rows = database.connection.execute(
        f"""SELECT id, unifi_id
            FROM vouchers
            WHERE controller_id=?
              AND archived_at IS NULL
              AND unifi_id IN ({placeholders})
            ORDER BY id""",
        (int(controller_id), *remote_ids),
    ).fetchall()

    resolved: dict[str, int] = {}
    for row in rows:
        remote_id = str(row["unifi_id"] or "").strip()
        if not remote_id or remote_id in resolved:
            raise RuntimeError(
                "Identità voucher locale ambigua. Sincronizzare e riprovare."
            )
        resolved[remote_id] = int(row["id"])

    if set(resolved) != set(remote_ids):
        raise RuntimeError(
            "Uno o più voucher selezionati non appartengono alla controller attiva "
            "o non sono presenti nello storico locale. Sincronizzare e riprovare."
        )

    return tuple(resolved[remote_id] for remote_id in remote_ids)


class LocalMetadataSelectionDialog(tk.Toplevel):
    """Select local-metadata targets independently from print/delete state."""

    def __init__(
        self,
        app,
        vouchers,
        *,
        title: str,
        multiple: bool,
    ):
        super().__init__(app)
        self.app = app
        self.vouchers = local_data_selection_candidates(vouchers)
        self.result = None
        self.multiple = bool(multiple)
        self.title(title)
        self.transient(app)
        self.grab_set()

        shell = ttk.Frame(self, padding=18)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text=title,
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "Questa selezione è separata da stampa ed eliminazione. "
                "Sono inclusi anche i voucher scaduti perché nominalità e "
                "note locali servono alla reportistica storica."
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
            selectmode="extended" if self.multiple else "browse",
        )
        self.tree.heading("voucher", text="Voucher")
        self.tree.heading("description", text="Destinatario UniFi")
        self.tree.heading("status", text="Stato")
        self.tree.column("voucher", width=180)
        self.tree.column("description", width=380)
        self.tree.column("status", width=140, anchor="center")

        preselected = {
            str(value)
            for value in getattr(app, "checked_ids", set())
        }
        selected_iids: list[str] = []
        for voucher in self.vouchers:
            iid = str(voucher.id)
            expired = str(
                getattr(voucher, "status", "") or ""
            ).strip().upper() == "EXPIRED"
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    str(getattr(voucher, "code_formatted", "") or ""),
                    str(getattr(voucher, "recipient", "") or "") or "—",
                    "Scaduto" if expired else "Attivo",
                ),
            )
            if iid in preselected:
                selected_iids.append(iid)

        if selected_iids:
            if self.multiple:
                self.tree.selection_set(selected_iids)
            else:
                self.tree.selection_set(selected_iids[:1])
                self.tree.focus(selected_iids[0])

        actions = ttk.Frame(shell)
        actions.pack(side="bottom", fill="x", pady=(12, 0))
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

        self.tree.pack(fill="both", expand=True)
        fit_toplevel_to_content(
            self,
            preferred_width=820,
            preferred_height=500,
            min_width=700,
            min_height=420,
        )

        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    def _accept(self) -> None:
        selected = set(self.tree.selection())
        if not selected:
            messagebox.showinfo(
                self.title(),
                "Selezionare almeno un voucher.",
                parent=self,
            )
            return
        result = tuple(
            voucher
            for voucher in self.vouchers
            if str(voucher.id) in selected
        )
        if not self.multiple and len(result) != 1:
            messagebox.showinfo(
                self.title(),
                "Selezionare un solo voucher.",
                parent=self,
            )
            return
        self.result = result
        self.destroy()


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
            self.destroy()
            refreshed = self.app._finalize_voucher_operation_ui(
                operation="nominality",
            )
            if not refreshed:
                return
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
            self.destroy()
            refreshed = self.app._finalize_voucher_operation_ui(
                operation="notes",
            )
            if not refreshed:
                return
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

    def __init__(self, app, *, target_unifi_ids=None):
        super().__init__(app)
        self.app = app
        self._target_unifi_ids = {
            str(value).strip()
            for value in (target_unifi_ids or ())
            if str(value).strip()
        }
        self.nominal = tk.StringVar(value="")
        self.print_state = tk.StringVar(value="")
        self.status = tk.StringVar()
        self._candidates = {}
        self.title("Allinea voucher")
        self.transient(app)
        self.grab_set()

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
                "o più voucher e indica la nominalità. Una stampa verificata "
                "nello storico prevale; in assenza di prova positiva, un voucher "
                "trovato sulla controller viene allineato come Non stampato."
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
        self.tree.bind("<<TreeviewSelect>>", self._sync_alignment_fields)

        footer = ttk.Frame(shell)
        footer.pack(side="bottom", fill="x")

        fields = ttk.Frame(footer)
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

        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel").pack(
            anchor="w", pady=(10, 0)
        )
        actions = ttk.Frame(footer)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="Chiudi", command=self.destroy).pack(side="right")
        self.save_button = ttk.Button(
            actions,
            text="Allinea selezionati",
            style="Accent.TButton",
            command=self._save,
        )
        self.save_button.pack(side="right", padx=(0, 8))
        self.delete_invalid_button = ttk.Button(
            actions,
            text="Elimina non validi…",
            command=self._delete_invalid_selected,
        )
        self.delete_invalid_button.pack(side="left")

        self.tree.pack(fill="both", expand=True)
        self._refresh()
        fit_toplevel_to_content(
            self,
            preferred_width=850,
            preferred_height=540,
            min_width=720,
            min_height=460,
        )

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
        if self._target_unifi_ids:
            candidates = tuple(
                item
                for item in candidates
                if str(item.unifi_id) in self._target_unifi_ids
            )
        self._candidates = {str(item.voucher_id): item for item in candidates}
        selected_iids = []
        for item in candidates:
            if item.last_printed_at:
                print_label = f"Verificata {item.last_printed_at[:10]}"
            elif str(item.print_state or "").strip().upper() == PRINT_STATE_PRINTED:
                print_label = "Stampato • data non determinabile"
            elif str(item.origin or "").strip().upper() == "CONTROLLER":
                print_label = "Nessuna stampa verificata"
            else:
                print_label = "Da dichiarare"
            iid = str(item.voucher_id)
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    item.name or "—",
                    item.created_at[:10] if item.created_at else "—",
                    print_label,
                ),
            )
            if self._target_unifi_ids:
                selected_iids.append(iid)

        if selected_iids:
            self.tree.selection_set(selected_iids)
            self.tree.focus(selected_iids[0])

        self.status.set(
            f"{len(candidates)} voucher da allineare."
            if candidates
            else "Nessun voucher da allineare."
        )
        self.save_button.state(["!disabled"] if candidates else ["disabled"])
        self.delete_invalid_button.state(
            ["!disabled"] if candidates else ["disabled"]
        )
        self.nominal.set("")
        self.print_state.set("")
        self.nominal_combo.configure(state="readonly")
        self.print_combo.configure(state="readonly")
        if selected_iids:
            self._sync_alignment_fields()

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
        elif not any(positive_print) and origins == {"CONTROLLER"}:
            self.print_state.set("Non stampato")
            self.print_combo.configure(state="disabled")
        else:
            self.print_state.set("")
            self.print_combo.configure(state="readonly")

    def _delete_invalid_selected(self) -> None:
        """Use the reviewed delete workflow for external blank-name vouchers."""

        selected = [
            self._candidates[value]
            for value in self.tree.selection()
            if value in self._candidates
        ]
        if not selected:
            messagebox.showinfo(
                "Elimina voucher non valido",
                "Selezionare almeno un voucher da allineare.",
                parent=self,
            )
            return

        invalid = [
            item
            for item in selected
            if str(item.origin or "").strip().upper() == "CONTROLLER"
            and not str(item.name or "").strip()
        ]
        if len(invalid) != len(selected):
            messagebox.showinfo(
                "Elimina voucher non valido",
                "Questo percorso è riservato ai voucher trovati sulla controller "
                "senza destinatario/descrizione UniFi. Gli altri voucher devono "
                "essere allineati normalmente.",
                parent=self,
            )
            return

        remote_ids = {str(item.unifi_id) for item in invalid}
        live = [
            voucher
            for voucher in getattr(self.app, "vouchers", ())
            if str(getattr(voucher, "id", "")) in remote_ids
        ]
        if len(live) != len(remote_ids):
            messagebox.showwarning(
                "Elimina voucher non valido",
                "Uno o più voucher non sono presenti nella fotografia live. "
                "Eseguire Sincronizza e riprovare.",
                parent=self,
            )
            return

        request_delete = getattr(
            self.app,
            "_request_delete_vouchers",
            None,
        )
        if not callable(request_delete):
            messagebox.showerror(
                "Elimina voucher non valido",
                "Il workflow di cancellazione sicura non è disponibile.",
                parent=self,
            )
            return

        # Release the alignment grab before the shared deletion workflow opens
        # its own reason/confirmation dialogs.
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        request_delete(live)

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

        controller_without_positive_print = [
            item
            for item, has_positive_print in zip(
                selected_candidates,
                positive_print_flags,
                strict=True,
            )
            if str(item.origin or "").strip().upper() == "CONTROLLER"
            and not has_positive_print
        ]
        if (
            controller_without_positive_print
            and requested_print != PRINT_STATE_NOT_PRINTED
        ):
            messagebox.showerror(
                "Allinea voucher",
                "I voucher trovati sulla controller senza una stampa verificata "
                "devono essere allineati come Non stampati.",
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
            refreshed = self.app._finalize_voucher_operation_ui(
                operation="alignment",
            )
            self._refresh()
            if not refreshed:
                return
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

    def _select_local_metadata_vouchers(
        self,
        *,
        title: str,
        multiple: bool,
    ) -> tuple:
        selected = selected_workspace_vouchers(self)
        if not selected:
            messagebox.showinfo(
                title,
                "Selezionare prima uno o più voucher nella tabella Voucher.",
                parent=self,
            )
            return ()
        if not multiple and len(selected) != 1:
            messagebox.showinfo(
                title,
                "Questa funzione richiede un solo voucher selezionato.",
                parent=self,
            )
            return ()
        return tuple(selected)

    def voucher_action_states(self) -> dict[str, bool]:
        """Expose dynamic menu availability to the Voucher workspace."""

        return voucher_action_states(self)

    def edit_selected_nominality(self) -> None:
        vouchers = self._select_local_metadata_vouchers(
            title="Nominalità voucher",
            multiple=True,
        )
        if vouchers:
            NominalityDialog(self, vouchers)

    def edit_selected_notes(self) -> None:
        vouchers = self._select_local_metadata_vouchers(
            title="Note voucher",
            multiple=False,
        )
        if vouchers:
            NotesDialog(self, vouchers)

    def align_pending_vouchers(self) -> None:
        if getattr(self, "active_controller_id", None) is None:
            messagebox.showinfo(
                "Allinea voucher",
                "Connettersi e sincronizzare la controller prima di allineare i voucher.",
                parent=self,
            )
            return

        selected = selected_workspace_vouchers(self)
        if not selected:
            messagebox.showinfo(
                "Allinea voucher",
                "Selezionare prima uno o più voucher nella tabella Voucher.",
                parent=self,
            )
            return

        states = voucher_action_states(self)
        if not states["align"]:
            messagebox.showinfo(
                "Allinea voucher",
                (
                    "La selezione contiene voucher già allineati oppure voucher "
                    "che non possono essere allineati insieme. Selezionare solo "
                    "voucher con stato “DA ALLINEARE”."
                ),
                parent=self,
            )
            return

        AlignmentDialog(
            self,
            target_unifi_ids=tuple(
                str(getattr(voucher, "id", "") or "")
                for voucher in selected
            ),
        )
