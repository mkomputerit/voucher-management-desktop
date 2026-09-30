"""Operator workflow for local-only voucher metadata.

Controller-owned fields are displayed read-only.  The only writable values are
administrative facts stored in the local SQLite archive; no UniFi API mutation
is performed by this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
import tkinter as tk
from tkinter import messagebox, ttk

from .database import VoucherLocalMetadata


CLASSIFICATION_LABELS = (
    "Non classificato",
    "Non nominale",
    "Nominale",
)


def classification_label(
    value: bool | None,
    *,
    redacted: bool = False,
) -> str:
    """Return the operator-facing local nominality label."""

    if redacted:
        return "Rimossa per privacy"
    if value is True:
        return "Nominale"
    if value is False:
        return "Non nominale"
    return "Non classificato"


def classification_value(label: str) -> bool | None:
    """Convert one editable classification label to its stored tri-state."""

    normalized = str(label or "").strip()
    if normalized == "Nominale":
        return True
    if normalized == "Non nominale":
        return False
    if normalized == "Non classificato":
        return None
    raise ValueError("Classificazione locale non valida.")


def local_metadata_summary(metadata: VoucherLocalMetadata | None) -> str:
    """Compact text for the voucher list without hiding the UniFi description."""

    if metadata is None:
        return "Dati locali non disponibili"
    label = classification_label(
        metadata.is_nominal,
        redacted=metadata.nominality_redacted,
    )
    recipient = metadata.assigned_to.strip()
    return f"{recipient} · {label}" if recipient else label


def origin_label(origin: str) -> str:
    return (
        "Creato con Voucher Management"
        if str(origin or "").strip() == "APPLICATION"
        else "Rilevato su UniFi · creatore non verificato"
    )


class VoucherLocalMetadataDialog(tk.Toplevel):
    """Edit only data that Voucher Management owns locally."""

    def __init__(
        self,
        parent,
        *,
        voucher,
        metadata: VoucherLocalMetadata,
    ):
        super().__init__(parent)
        self.title("Dati locali voucher")
        self.transient(parent)
        self.grab_set()
        self.resizable(True, False)
        self.result: tuple[str, str, bool | None] | None = None

        self.assigned_to_var = tk.StringVar(value=metadata.assigned_to)
        self.classification_var = tk.StringVar(
            value=(
                "Non classificato"
                if metadata.nominality_redacted
                else classification_label(metadata.is_nominal)
            )
        )

        shell = ttk.Frame(self, padding=20)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text="Dati locali voucher",
            style="PageTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "I dati UniFi sotto sono in sola lettura. Salva modifica soltanto "
                "le informazioni amministrative locali usate da Voucher Management "
                "e dai report."
            ),
            style="Muted.TLabel",
            wraplength=640,
        ).pack(anchor="w", pady=(3, 14))

        remote = ttk.Labelframe(
            shell,
            text="Dati UniFi · sola lettura",
            padding=(14, 10),
        )
        remote.pack(fill="x")
        self._readonly_row(remote, 0, "Voucher", voucher.code_formatted)
        self._readonly_row(
            remote,
            1,
            "Descrizione UniFi",
            voucher.recipient or "—",
        )
        self._readonly_row(remote, 2, "ID UniFi", str(voucher.id))
        self._readonly_row(remote, 3, "Origine", origin_label(metadata.origin))

        local = ttk.Labelframe(
            shell,
            text="Informazioni locali",
            padding=(14, 10),
        )
        local.pack(fill="x", pady=(14, 0))
        local.columnconfigure(1, weight=1)

        ttk.Label(local, text="Destinatario locale").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Entry(
            local,
            textvariable=self.assigned_to_var,
            width=48,
        ).grid(row=0, column=1, sticky="ew", pady=5)

        ttk.Label(local, text="Classificazione").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=5
        )
        ttk.Combobox(
            local,
            textvariable=self.classification_var,
            state="readonly",
            values=CLASSIFICATION_LABELS,
            width=22,
        ).grid(row=1, column=1, sticky="w", pady=5)

        ttk.Label(local, text="Note interne").grid(
            row=2, column=0, sticky="nw", padx=(0, 12), pady=5
        )
        self.notes_text = tk.Text(local, height=5, width=52, wrap="word")
        self.notes_text.grid(row=2, column=1, sticky="ew", pady=5)
        self.notes_text.insert("1.0", metadata.notes)

        ttk.Label(
            local,
            text=(
                "Il destinatario locale e le note non vengono inviati a UniFi. "
                "La descrizione UniFi, il codice, l'ID, la durata, gli utilizzi "
                "e la scadenza non sono modificabili da questa schermata."
            ),
            style="Muted.TLabel",
            wraplength=600,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        footer = ttk.Frame(shell)
        footer.pack(fill="x", pady=(18, 0))
        ttk.Button(
            footer,
            text="Annulla",
            command=self.destroy,
            width=12,
        ).pack(side="right")
        ttk.Button(
            footer,
            text="Salva dati locali",
            command=self._save,
            style="Accent.TButton",
            width=18,
        ).pack(side="right", padx=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        width = max(650, self.winfo_reqwidth() + 30)
        height = max(480, self.winfo_reqheight() + 30)
        self.geometry(f"{width}x{height}")

    @staticmethod
    def _readonly_row(parent, row: int, label: str, value: str) -> None:
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 12),
            pady=3,
        )
        ttk.Label(parent, text=value, wraplength=480).grid(
            row=row,
            column=1,
            sticky="w",
            pady=3,
        )
        parent.columnconfigure(1, weight=1)

    def _save(self) -> None:
        assigned_to = self.assigned_to_var.get().strip()
        notes = self.notes_text.get("1.0", "end-1c").strip()
        if len(assigned_to) > 200:
            messagebox.showerror(
                "Dati locali",
                "Il destinatario locale non può superare 200 caratteri.",
                parent=self,
            )
            return
        if len(notes) > 2000:
            messagebox.showerror(
                "Dati locali",
                "Le note interne non possono superare 2000 caratteri.",
                parent=self,
            )
            return
        try:
            nominal = classification_value(self.classification_var.get())
        except ValueError as exc:
            messagebox.showerror("Dati locali", str(exc), parent=self)
            return
        if nominal is True and not assigned_to:
            messagebox.showerror(
                "Dati locali",
                "Per un voucher nominale indicare il destinatario locale.",
                parent=self,
            )
            return
        self.result = (assigned_to, notes, nominal)
        self.destroy()


class VoucherLocalMetadataMixin:
    """Expose local administrative enrichment without mutating UniFi."""

    def _voucher_for_local_metadata(self):
        iid = self.tree.focus() if hasattr(self, "tree") else ""
        voucher = self.by_iid.get(iid) if iid else None
        if voucher is not None:
            return voucher
        selected_ids = {
            str(value)
            for value in getattr(self, "checked_ids", set())
        }
        matches = [
            item for item in getattr(self, "vouchers", [])
            if str(item.id) in selected_ids
        ]
        return matches[0] if len(matches) == 1 else None

    def edit_local_voucher_metadata(self) -> None:
        """Edit one voucher's local classification/notes using stable UniFi id."""

        if getattr(self, "_background_results", None) is not None:
            self.bell()
            return

        controller_id = getattr(self, "active_controller_id", None)
        if controller_id is None:
            messagebox.showinfo(
                "Dati locali",
                "Connettersi al controller e sincronizzare prima di modificare "
                "i dati locali di un voucher.",
                parent=self,
            )
            return
        if not bool(getattr(self, "controller_snapshot_live", False)):
            messagebox.showwarning(
                "Dati locali",
                "L'elenco UniFi non è aggiornato. Eseguire Sincronizza con successo "
                "prima di modificare i dati locali.",
                parent=self,
            )
            return

        voucher = self._voucher_for_local_metadata()
        if voucher is None:
            messagebox.showinfo(
                "Dati locali",
                "Fare clic sulla riga di un voucher, quindi scegliere Dati locali…",
                parent=self,
            )
            return

        metadata = self.database.voucher_local_metadata(
            controller_id=int(controller_id),
            unifi_id=str(voucher.id),
        )
        if metadata is None or not metadata.present_on_controller:
            messagebox.showwarning(
                "Dati locali",
                "Il voucher non è disponibile nello snapshot locale. "
                "Eseguire Sincronizza e riprovare.",
                parent=self,
            )
            return

        dialog = VoucherLocalMetadataDialog(
            self,
            voucher=voucher,
            metadata=metadata,
        )
        self.wait_window(dialog)
        if dialog.result is None:
            return

        assigned_to, notes, nominal = dialog.result
        try:
            self.database.update_voucher_local_metadata(
                controller_id=int(controller_id),
                unifi_id=str(voucher.id),
                assigned_to=assigned_to,
                notes=notes,
                is_nominal=nominal,
                updated_at=datetime.now(timezone.utc).isoformat(),
                windows_user=self._windows_operator_identity(),
            )
        except (RuntimeError, ValueError) as exc:
            messagebox.showerror(
                "Dati locali",
                str(exc),
                parent=self,
            )
            return

        # A successfully handled voucher leaves the operational selection.
        # Other selected vouchers stay selected so batch work is not disrupted.
        self.checked_ids.discard(voucher.id)
        self.populate()
        refresh_reports = getattr(self, "_refresh_report_summary", None)
        if refresh_reports is not None:
            refresh_reports()
        messagebox.showinfo(
            "Dati locali",
            "Informazioni locali aggiornate. I dati UniFi sono rimasti invariati.",
            parent=self,
        )
