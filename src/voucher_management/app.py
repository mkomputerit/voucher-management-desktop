"""Shared application workflow used by the Windows operator UI.

This module deliberately owns voucher creation, PDF generation and print/audit
workflow while presentation lives in modern_app.py. Keeping that boundary
isolates the official controller API from the tested print path.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from . import __version__
from .history import HistoryError, HistoryService
from .identity import PRODUCT_NAME
from .logging_utils import configure_logging
from .models import VoucherBatch, VoucherRecord
from .paths import AppPaths
from .pdf_preview import PdfPreview
from .pdf_render import VOUCHERS_PER_PAGE, render_batch_pdf
from .settings import SettingsStore
from .security.history_key import HistoryKeyStore
from .unifi_api import ApiVoucher, UniFiApiError
from .utils import (
    find_file_by_exact_name,
    sanitize_filename_component,
    unique_output_path,
)


def bundled_app_icon_path() -> Path | None:
    """Return the bundled Windows icon path when running from PyInstaller."""

    bundle_root = getattr(sys, "_MEIPASS", None)
    if not bundle_root:
        return None
    candidate = Path(bundle_root) / "assets" / "VoucherManagement.ico"
    return candidate if candidate.is_file() else None


def duration_label(minutes: int) -> str:
    if minutes and minutes % 1440 == 0:
        n = minutes // 1440
        return f"{n} giorno" if n == 1 else f"{n} giorni"
    if minutes and minutes % 60 == 0:
        n = minutes // 60
        return f"{n} ora" if n == 1 else f"{n} ore"
    return f"{minutes} min"


def status_label(status: str) -> str:
    return {"VALID_MULTI": "Disponibile", "USED_MULTIPLE": "In uso", "EXPIRED": "Scaduto"}.get(status, status or "-")


def time_label(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M") if ts else "-"


class CreateDialog(tk.Toplevel):
    """Collect the controller parameters required to create a voucher batch."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Aggiungi nuovo voucher")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.name = tk.StringVar()
        self.qty = tk.IntVar(value=1)
        self.mode = tk.StringVar(value="Monouso")
        self.quota = tk.IntVar(value=2)
        self.expire = tk.IntVar(value=24)
        self.unit = tk.StringVar(value="Ore")
        self.data = tk.StringVar()
        self.down = tk.StringVar()
        self.up = tk.StringVar()
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        for r, (label, widget) in enumerate((("Nome", ttk.Entry(f, textvariable=self.name, width=34)), ("Quantità", ttk.Spinbox(f, from_=1, to=50, textvariable=self.qty, width=8)))):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
            widget.grid(row=r, column=1, sticky="ew", pady=4)
        ttk.Label(f, text="Utilizzo").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.mode, state="readonly", values=("Monouso", "Multiuso", "Multiuso illimitato"), width=22).grid(row=2, column=1, sticky="w")
        ttk.Label(f, text="Numero utilizzi (Multiuso)").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Spinbox(f, from_=2, to=999, textvariable=self.quota, width=8).grid(row=3, column=1, sticky="w")
        ttk.Label(f, text="Scadenza").grid(row=4, column=0, sticky="w", pady=4)
        ef = ttk.Frame(f)
        ef.grid(row=4, column=1, sticky="w")
        ttk.Spinbox(ef, from_=1, to=9999, textvariable=self.expire, width=8).pack(side="left")
        ttk.Combobox(ef, textvariable=self.unit, state="readonly", values=("Minuti", "Ore", "Giorni"), width=10).pack(side="left", padx=6)
        for r, (label, var) in enumerate((("Limite dati MB (vuoto = illimitato)", self.data), ("Download Mbps (vuoto = illimitato)", self.down), ("Upload Mbps (vuoto = illimitato)", self.up)), 5):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
            ttk.Entry(f, textvariable=var, width=12).grid(row=r, column=1, sticky="w")
        b = ttk.Frame(f)
        b.grid(row=8, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(b, text="Annulla", command=self.destroy).pack(side="left", padx=5)
        ttk.Button(b, text="Aggiungi", command=self.accept).pack(side="left")
        self.wait_window(self)

    def accept(self):
        try:
            name = self.name.get().strip()
            qty = int(self.qty.get())
            exp = int(self.expire.get())
            if not name or not 1 <= qty <= 50 or exp < 1:
                raise ValueError
            mode = self.mode.get()
            quota = 1 if mode == "Monouso" else (0 if mode == "Multiuso illimitato" else int(self.quota.get()))
            if mode == "Multiuso" and not 2 <= quota <= 999:
                raise ValueError
            unit = {"Minuti": 1, "Ore": 60, "Giorni": 1440}[self.unit.get()]
            def opt(v):
                s = v.get().strip()
                return None if not s else int(s)
            data, down, up = opt(self.data), opt(self.down), opt(self.up)
            if any(x is not None and x <= 0 for x in (data, down, up)):
                raise ValueError
            # Official Network API rate limits are documented in Kbps with a
            # maximum of 100,000, therefore the UI's Mbps values stop at 100.
            if down is not None and down > 100:
                raise ValueError
            if up is not None and up > 100:
                raise ValueError
            if data is not None and data > 1_048_576:
                raise ValueError
            self.result = dict(recipient=name, quantity=qty, expire_number=exp, expire_unit=unit, quota=quota, data_mb=data, down_mbps=down, up_mbps=up)
        except (KeyError, TypeError, ValueError, tk.TclError):
            messagebox.showerror(
                "Voucher",
                "Controllare i valori inseriti",
                parent=self,
            )
            return
        self.destroy()


class PrintCopiesDialog(tk.Toplevel):
    """Ask how many physical labels to place in the PDF for one unlimited code."""

    def __init__(self, parent, voucher: ApiVoucher):
        super().__init__(parent)
        self.title("Copie voucher")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.copies = tk.IntVar(value=1)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=f"Voucher {voucher.code_formatted} - utilizzo illimitato").pack(anchor="w")
        ttk.Label(f, text="Numero di copie fisiche da preparare:").pack(anchor="w", pady=(10, 4))
        ttk.Spinbox(f, from_=1, to=999, textvariable=self.copies, width=8).pack(anchor="w")
        ttk.Label(f, text=f"Massimo {VOUCHERS_PER_PAGE} voucher per pagina; le pagine aggiuntive sono automatiche.", foreground="#666").pack(anchor="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.pack(anchor="e", pady=(14, 0))
        ttk.Button(b, text="Annulla", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(b, text="Continua", command=self.accept).pack(side="left")
        self.wait_window(self)

    def accept(self):
        try:
            n = int(self.copies.get())
            if not 1 <= n <= 999:
                raise ValueError("copies out of range")
        except (TypeError, ValueError, tk.TclError):
            messagebox.showerror("Copie", "Inserire un numero tra 1 e 999", parent=self)
            return
        self.result = n
        self.destroy()


class VoucherApp(tk.Tk):
    """Shared application state and stable voucher/print workflow methods."""

    def __init__(self):
        super().__init__()
        icon_path = bundled_app_icon_path()
        if icon_path is not None:
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                # The EXE resource still carries the application icon even if a
                # particular Tk build cannot apply iconbitmap at runtime.
                pass
        self.title(f"{PRODUCT_NAME} {__version__}")
        self.minsize(1120, 650)
        self.geometry("1420x780")
        self.paths = AppPaths()
        try:
            self.paths.ensure_writable()
        except Exception as exc:
            messagebox.showerror("Avvio impossibile", f"Cartella dell'applicazione non scrivibile.\n\n{exc}")
            self.destroy()
            return
        self.settings_store = SettingsStore(self.paths.settings)
        self.settings = self.settings_store.load()
        settings_warning = self.settings_store.consume_warning()

        migrated_logo = self.paths.persist_configured_logo(
            self.settings.get("logo_path", "")
        )
        if migrated_logo != self.settings.get("logo_path", ""):
            self.settings["logo_path"] = migrated_logo
            self.settings_store.save(self.settings)

        self.logger = configure_logging(
            self.paths.logs,
            int(self.settings.get("log_retention_days", 30)),
        )
        self.history = HistoryService(
            self.paths.history,
            self.paths.history_lock,
            self.settings_store,
            secret_store=HistoryKeyStore(
                self.paths.user_root,
                legacy_roots=self.paths.legacy_user_roots,
            ),
        )
        # HistoryService can initialize or repair the fingerprint on disk.
        # Keep the UI copy synchronized so later saves cannot erase it.
        self.settings = self.settings_store.load()
        if settings_warning:
            messagebox.showwarning(
                "Impostazioni ripristinate",
                settings_warning,
                parent=self,
            )
        self.client = None
        self.vouchers = []
        self.by_iid = {}
        self.checked_ids = set()
        self.last_pdf = None
        # Official API credentials are deliberately split from persistent
        # configuration: the API root/certificate pin may be saved, while the
        # API key lives only in this Tk variable and the connected client.
        self.api_root_var = tk.StringVar(
            value=self.settings.get("controller_api_root", "")
        )
        self.api_key_var = tk.StringVar()
        self.connection_var = tk.StringVar(value="Non connesso")
        # Default to the operator's real task rather than the complete archive.
        self.filter_var = tk.StringVar(value="Da stampare")
        self.search_var = tk.StringVar()
        self.count_var = tk.StringVar(value="0 voucher")
        self.action_var = tk.StringVar(value="PREPARA STAMPA")
        self._build_ui()

    def _build_ui(self) -> None:
        """Build the concrete UI.

        Presentation lives in ModernVoucherApp. Keeping this base method
        abstract prevents a second, stale interface from diverging again.
        """
        raise NotImplementedError

    @staticmethod
    def _is_expired(voucher: ApiVoucher) -> bool:
        """Use the controller lifecycle state, never a translated UI label."""

        return voucher.status == "EXPIRED"

    def report_callback_exception(
        self,
        exc_type,
        exc_value,
        exc_traceback,
    ) -> None:
        """Surface unexpected Tk callback failures in a console-less build.

        Tracebacks are written to the local diagnostic log for review, while
        the operator receives a generic dialog that does not echo potentially
        sensitive exception contents.
        """
        # Do not persist exception text/traceback: callbacks can contain
        # controller URLs or other operator-entered values in local variables.
        self.logger.error(
            "unhandled_ui_callback type=%s",
            getattr(exc_type, "__name__", str(exc_type)),
        )
        try:
            messagebox.showerror(
                "Errore imprevisto",
                "Si è verificato un errore imprevisto nell'interfaccia. "
                "L'operazione è stata interrotta. Consultare il log locale "
                "prima di riprovare.",
                parent=self,
            )
        except Exception:
            # Tk may itself be tearing down; logging above remains available.
            pass

    @staticmethod
    def _print_state(stat) -> str:
        if not stat or not stat.generated_documents:
            return "DA STAMPARE"
        if not stat.print_jobs:
            return "PDF CREATO"
        return "STAMPATO"

    def connect(self) -> None:
        """Connect through the concrete network/UI adapter."""
        raise NotImplementedError

    def refresh(self):
        if not self.client:
            messagebox.showinfo("UniFi", "Connettersi prima al controller UniFi", parent=self)
            return
        try:
            self.vouchers = self.client.list_vouchers()
            self.populate()
        except UniFiApiError as exc:
            messagebox.showerror("Sincronizzazione", str(exc), parent=self)

    def populate(self) -> None:
        """Render vouchers in the concrete operator UI."""
        raise NotImplementedError

    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell" or self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if not iid or iid not in self.by_iid:
            return "break"
        voucher = self.by_iid[iid]
        if self._is_expired(voucher):
            self.bell()
            return "break"
        if voucher.id in self.checked_ids:
            self.checked_ids.remove(voucher.id)
        else:
            self.checked_ids.add(voucher.id)
        self.populate()
        return "break"

    def toggle_all_visible(self):
        # Expired rows may be visible in archive views but are never selectable.
        visible = {v.id for v in self.by_iid.values() if not self._is_expired(v)}
        if visible and visible.issubset(self.checked_ids):
            self.checked_ids.difference_update(visible)
        else:
            self.checked_ids.update(visible)
        self.populate()

    def select_unprinted(self) -> None:
        """Select the current print queue in the concrete UI."""
        raise NotImplementedError

    def selected(self) -> list[ApiVoucher]:
        # Defensive guard: printing/deletion must not rely only on UI checkbox state.
        return [v for v in self.vouchers if v.id in self.checked_ids and not self._is_expired(v)]

    def create(self):
        if not self.client:
            messagebox.showinfo(
                "UniFi",
                "Connettersi prima al controller UniFi",
                parent=self,
            )
            return
        dialog = CreateDialog(self)
        if not dialog.result:
            return

        try:
            created = self.client.create_vouchers(**dialog.result)
        except UniFiApiError as exc:
            messagebox.showerror("Creazione voucher", str(exc), parent=self)
            return

        self.checked_ids = {v.id for v in created}
        self.filter_var.set("Da stampare")

        try:
            self.vouchers = self.client.list_vouchers()
        except UniFiApiError as exc:
            existing = {voucher.id: voucher for voucher in self.vouchers}
            for voucher in created:
                existing[voucher.id] = voucher
            self.vouchers = list(existing.values())
            self.populate()
            messagebox.showwarning(
                "Voucher creati",
                f"Creati {len(created)} voucher, ma l'aggiornamento "
                f"dell'elenco non è riuscito. Non ripetere la creazione.\n\n{exc}",
                parent=self,
            )
            return

        self.populate()
        messagebox.showinfo(
            "Voucher",
            f"Creati {len(created)} voucher. Sono già selezionati per la stampa.",
            parent=self,
        )

    def print_selected(self):
        """Generate into Print automatically and immediately show the final PDF."""
        selected = self.selected()
        if not selected:
            messagebox.showinfo("Stampa", "Selezionare uno o più voucher attivi dalla prima colonna", parent=self)
            return
        try:
            self.history.stats_for_codes(
                [v.code_formatted for v in selected],
                self.settings,
            )
        except HistoryError as exc:
            messagebox.showerror(
                "Cronologia non disponibile",
                f"{exc}\n\nLa stampa viene sospesa per evitare un audit incompleto.",
                parent=self,
            )
            return
        copies = 1
        if len(selected) == 1 and selected[0].quota == 0:
            dialog = PrintCopiesDialog(self, selected[0])
            if dialog.result is None:
                return
            copies = dialog.result
        records = []
        for voucher in selected:
            repeat = copies if len(selected) == 1 and voucher.quota == 0 else 1
            records.extend(VoucherRecord(code=voucher.code_formatted, duration_minutes=voucher.duration_minutes, recipient=voucher.recipient or "Guest") for _ in range(repeat))
        batch = VoucherBatch(source_path=Path("CONTROLLER_API"), vouchers=records, recipient=records[0].recipient if records else "")
        now = datetime.now()
        folder = self.paths.prints / f"{now:%Y}" / f"{now:%m}"
        folder.mkdir(parents=True, exist_ok=True)
        base = sanitize_filename_component(batch.recipient or "Voucher") or "Voucher"
        output = unique_output_path(folder / f"Voucher_{base}_{now:%Y%m%d_%H%M%S}.pdf")
        try:
            render_batch_pdf(batch, output, self.settings)
            duplicates = bool(self.history.find_duplicates(batch.codes, self.settings))
            self.history.record_batch(batch, output, self.settings, reprint=duplicates)
            self.last_pdf = output
            self.populate()
            self._preview(output, batch.codes)
        except Exception as exc:
            self.logger.error(
                "pdf_generation_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror("Stampa", str(exc), parent=self)

    def _preview(self, path: Path, codes: list[str]):
        PdfPreview(self, path, codes, self.history, self.settings, on_print=self.populate)

    def open_existing_pdf(self):
        selected = self.selected()
        if len(selected) != 1:
            messagebox.showinfo("Apri PDF", "Selezionare un solo voucher attivo.", parent=self)
            return
        voucher = selected[0]
        try:
            stat = self.history.stats_for_codes(
                [voucher.code_formatted],
                self.settings,
            ).get(voucher.code_formatted)
        except HistoryError as exc:
            messagebox.showerror(
                "Cronologia non disponibile",
                str(exc),
                parent=self,
            )
            return
        if not stat or not stat.latest_output_file:
            messagebox.showinfo("Apri PDF", "Per questo voucher non risulta alcun PDF archiviato.", parent=self)
            return
        path = Path(stat.latest_output_file)
        if not path.is_absolute() or not path.exists():
            # New history stores only the portable archive filename. Older
            # history may contain an absolute path from another PC/profile.
            # In both cases resolve the definitive document inside Print.
            matches = sorted(
                find_file_by_exact_name(self.paths.prints, path.name),
                key=lambda item: item.stat().st_mtime,
            )
            path = matches[-1] if matches else path
        if not path.exists():
            messagebox.showerror("Apri PDF", f"Il file registrato nello storico non è più disponibile:\n{path}", parent=self)
            return
        # Reopening one voucher may resolve to a PDF containing many voucher
        # labels. Printing that PDF must mark every currently-known voucher
        # linked to the file, not only the row that the operator selected.
        try:
            linked_codes = self.history.codes_for_output(
                [item.code_formatted for item in self.vouchers],
                path,
                self.settings,
            )
        except HistoryError as exc:
            messagebox.showerror(
                "Cronologia non disponibile",
                str(exc),
                parent=self,
            )
            return
        if voucher.code_formatted not in linked_codes:
            # Fail closed rather than opening a document whose audit linkage no
            # longer matches the selected voucher.
            messagebox.showerror(
                "Apri PDF",
                "Il collegamento tra voucher e PDF non è verificabile nello "
                "storico locale.",
                parent=self,
            )
            return
        try:
            self._preview(path, linked_codes)
        except Exception as exc:
            self.logger.error(
                "pdf_preview_open_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Apri PDF",
                "Impossibile aprire l'anteprima del PDF selezionato.",
                parent=self,
            )

    def delete_selected(self) -> None:
        """Apply deletion policy in the concrete UI."""
        raise NotImplementedError
