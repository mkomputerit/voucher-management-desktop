"""Windows operator interface for Voucher Management."""

from __future__ import annotations

import logging
import shutil
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import darkdetect
import sv_ttk

from .app import VoucherApp, duration_label, time_label
from .history import HistoryError
from .identity import (
    DEFAULT_STRUCTURE_NAME,
    DEFAULT_STRUCTURE_TYPE,
    DEFAULT_WIFI_TITLE,
    PRODUCT_NAME,
)
from .logo_validation import LogoValidationError, validate_logo_image
from .pdf_render import VOUCHERS_PER_PAGE
from .print_archive import DEFAULT_PRINT_RETENTION_DAYS
from .utils import format_fingerprint
from .data_maintenance_ui import DataMaintenanceMixin
from .controller_connection_ui import ControllerConnectionMixin
from .voucher_deletion_ui import VoucherDeletionMixin


def audit_time_label(value: str) -> str:
    """Format an ISO audit timestamp for the compact operator table."""
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return "—"


def fit_dialog(window: tk.Toplevel, parent: tk.Misc, *, min_width: int = 0, min_height: int = 0) -> None:
    """Size a dialog from its requested layout instead of hard-coded pixels.

    Windows display scaling and ttk themes change the space requested by text,
    tabs and buttons. Measuring after layout keeps complete action rows visible
    at 100/125/150% scaling while still centring the dialog on its parent.
    """
    window.update_idletasks()
    width = max(min_width, window.winfo_reqwidth() + 24)
    height = max(min_height, window.winfo_reqheight() + 24)
    screen_w, screen_h = window.winfo_screenwidth(), window.winfo_screenheight()
    width = min(width, max(420, screen_w - 80))
    height = min(height, max(320, screen_h - 100))
    parent.update_idletasks()
    x = max(0, parent.winfo_rootx() + (parent.winfo_width() - width) // 2)
    y = max(0, parent.winfo_rooty() + (parent.winfo_height() - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")
    window.minsize(min(width, screen_w - 40), min(height, screen_h - 60))


class SettingsDialog(tk.Toplevel):
    """Operator settings grouped like a native management console."""

    def __init__(self, app: "ModernVoucherApp"):
        super().__init__(app)
        self.app = app
        self.title("Impostazioni")
        self.transient(app)
        self.grab_set()
        s = app.settings
        self.structure_type = tk.StringVar(value=s.get("structure_type", DEFAULT_STRUCTURE_TYPE))
        self.structure_name = tk.StringVar(value=s.get("structure_name", DEFAULT_STRUCTURE_NAME))
        self.wifi_title = tk.StringVar(value=s.get("wifi_title", DEFAULT_WIFI_TITLE))
        self.preset = tk.StringVar(value=s.get("preset", "Classico"))
        self.logo = tk.StringVar(value=s.get("logo_path", ""))
        self.theme = tk.StringVar(value=s.get("ui_theme", "system"))
        try:
            retention_days = int(s.get("print_retention_days", DEFAULT_PRINT_RETENTION_DAYS))
        except (TypeError, ValueError):
            retention_days = DEFAULT_PRINT_RETENTION_DAYS
        self.print_retention_days = tk.StringVar(value=str(retention_days))

        shell = ttk.Frame(self, padding=20)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="Impostazioni", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(shell, text="Voucher, aspetto dell'applicazione e protezione dei dati.", style="Muted.TLabel").pack(anchor="w", pady=(2, 14))
        tabs = ttk.Notebook(shell)
        tabs.pack(fill="both", expand=True)
        voucher = ttk.Frame(tabs, padding=20)
        data = ttk.Frame(tabs, padding=20)
        tabs.add(voucher, text="Voucher")
        tabs.add(data, text="Aspetto e dati")
        self._build_voucher_tab(voucher)
        self._build_data_tab(data)
        # Footer is structurally outside the expanding notebook: it can never be
        # squeezed out by tab content when Windows DPI scaling increases.
        footer = ttk.Frame(shell)
        footer.pack(fill="x", pady=(16, 0))
        ttk.Button(footer, text="Annulla", command=self.destroy, width=12).pack(side="right")
        ttk.Button(footer, text="Salva", command=self.save, style="Accent.TButton", width=12).pack(side="right", padx=(0, 8))
        fit_dialog(self, app, min_width=720, min_height=560)

    def _build_voucher_tab(self, frame: ttk.Frame) -> None:
        rows = (
            ("Profilo struttura", self.structure_type),
            ("Nome struttura", self.structure_name),
            ("Titolo Wi-Fi", self.wifi_title),
        )
        profile_values = ("Sede", "Evento", "Personalizzata")
        current_profile = self.structure_type.get().strip()
        if current_profile and current_profile not in profile_values:
            # Preserve a profile label imported from a private beta without
            # hard-coding deployment-specific values into the public source.
            profile_values = (current_profile, *profile_values)

        for row, (label, var) in enumerate(rows):
            ttk.Label(
                frame,
                text=label,
            ).grid(
                row=row,
                column=0,
                sticky="w",
                pady=8,
                padx=(0, 20),
            )
            widget = (
                ttk.Combobox(
                    frame,
                    textvariable=var,
                    state="readonly",
                    values=profile_values,
                )
                if row == 0
                else ttk.Entry(frame, textvariable=var)
            )
            widget.grid(
                row=row,
                column=1,
                columnspan=2,
                sticky="ew",
                pady=8,
            )
        ttk.Label(frame, text="Preset grafico").grid(row=3, column=0, sticky="w", pady=8)
        ttk.Combobox(frame, textvariable=self.preset, state="readonly", values=("Classico", "Minimal", "Contrasto", "Personalizzato")).grid(row=3, column=1, columnspan=2, sticky="ew", pady=8)
        ttk.Label(frame, text="Logo").grid(row=4, column=0, sticky="w", pady=8)
        self.logo_combo = ttk.Combobox(frame, state="readonly")
        self.logo_combo.grid(row=4, column=1, sticky="ew", pady=8)
        self.logo_combo.bind("<<ComboboxSelected>>", self._select_logo)
        ttk.Button(frame, text="Aggiungi…", command=self.add_logo).grid(row=4, column=2, padx=(8, 0))
        ttk.Button(frame, text="Usa predefinito", command=self.use_default).grid(row=5, column=1, sticky="w")
        ttk.Label(frame, text=f"I loghi vengono conservati nella libreria persistente Loghi. Layout A4: {VOUCHERS_PER_PAGE} voucher per pagina.", style="Muted.TLabel", wraplength=500).grid(row=6, column=0, columnspan=3, sticky="w", pady=(18, 0))
        frame.columnconfigure(1, weight=1)
        self._refresh_logos()

    def _build_data_tab(self, frame: ttk.Frame) -> None:
        ttk.Label(frame, text="Tema dell'applicazione", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Sistema segue il tema chiaro/scuro di Windows 11 all'avvio.", style="Muted.TLabel").pack(anchor="w", pady=(2, 8))
        ttk.Combobox(frame, textvariable=self.theme, state="readonly", values=("system", "light", "dark"), width=18).pack(anchor="w")
        ttk.Separator(frame).pack(fill="x", pady=22)
        ttk.Label(frame, text="Archivio PDF", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "I PDF generati vengono conservati in Print/. "
                "La retention elimina solo PDF riconosciuti dalla cronologia; "
                "lo storico di stampa resta disponibile. 0 = conserva sempre."
            ),
            style="Muted.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(3, 8))
        retention_row = ttk.Frame(frame)
        retention_row.pack(anchor="w")
        ttk.Label(retention_row, text="Giorni di conservazione").pack(side="left")
        ttk.Spinbox(
            retention_row,
            from_=0,
            to=3650,
            increment=30,
            textvariable=self.print_retention_days,
            width=8,
        ).pack(side="left", padx=(10, 0))

        ttk.Separator(frame).pack(fill="x", pady=22)
        ttk.Label(
            frame,
            text="Cronologia di stampa",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "La chiave portabile e il fingerprint proteggono la continuità "
                "dello storico. Se l'identità non è disponibile, stampa ed "
                "eliminazione vengono bloccate finché il problema non è risolto."
            ),
            style="Muted.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(3, 10))
        identity_actions = ttk.Frame(frame)
        identity_actions.pack(anchor="w")
        ttk.Button(
            identity_actions,
            text="Verifica / recupera identità…",
            command=lambda: HistoryRecoveryDialog(self.app),
        ).pack(side="left")
        ttk.Button(
            identity_actions,
            text="Recupera stampa pendente…",
            command=lambda: self.app.recover_pending_print_audit(
                parent=self,
            ),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            identity_actions,
            text="Esporta cronologia…",
            command=lambda: self.app.export_history_exchange(
                parent=self,
            ),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            identity_actions,
            text="Importa cronologia…",
            command=lambda: self.app.import_history_exchange(
                parent=self,
            ),
        ).pack(side="left", padx=(8, 0))

        if getattr(self.app.paths, "shared_mode", False):
            ttk.Separator(frame).pack(fill="x", pady=22)
            ttk.Label(
                frame,
                text="Archivio condiviso Windows",
                style="SectionTitle.TLabel",
            ).pack(anchor="w")
            ttk.Label(
                frame,
                text=(
                    "Questa installazione usa un archivio comune del PC in "
                    "ProgramData. Se questo utente dispone ancora di dati della "
                    "precedente installazione per profilo, possono essere "
                    "trasferiti una sola volta prima che l'archivio condiviso "
                    "venga utilizzato operativamente."
                ),
                style="Muted.TLabel",
                wraplength=560,
            ).pack(anchor="w", pady=(3, 10))
            ttk.Button(
                frame,
                text="Migra dati di questo utente…",
                command=lambda: self.app.migrate_per_user_data_to_shared(
                    parent=self,
                ),
            ).pack(anchor="w")

        ttk.Separator(frame).pack(fill="x", pady=22)
        ttk.Label(
            frame,
            text="Migrazione storico 4.x",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "Importa in SQLite lo storico HMAC delle versioni 4.x. "
                "Vengono associate solo identità verificabili; gli eventi "
                "ambigui o non associabili restano conservati come evidenza. "
                "Prima di ogni migrazione viene creato un backup cifrato."
            ),
            style="Muted.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(3, 10))
        ttk.Button(
            frame,
            text="Analizza e migra storico 4.x…",
            command=lambda: self.app.migrate_legacy_history(parent=self),
        ).pack(anchor="w")

        ttk.Separator(frame).pack(fill="x", pady=22)
        ttk.Label(frame, text="Backup e ripristino", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Il backup comprende configurazione, storico, PDF generati, loghi e la chiave portabile della cronologia. La API key UniFi non viene mai salvata.", style="Muted.TLabel", wraplength=560).pack(anchor="w", pady=(3, 12))
        actions = ttk.Frame(frame)
        actions.pack(anchor="w")
        ttk.Button(
            actions,
            text="Crea backup…",
            command=lambda: self.app.create_backup(parent=self),
        ).pack(side="left")
        ttk.Button(
            actions,
            text="Ripristina backup…",
            command=lambda: self.app.restore_backup(parent=self),
        ).pack(side="left", padx=(8, 0))

    def _logo_files(self) -> list[Path]:
        return sorted((p for p in self.app.paths.logos.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg"}), key=lambda p: p.name.lower())

    def _refresh_logos(self, select: Path | None = None) -> None:
        files = self._logo_files()
        self.logo_combo["values"] = [p.name for p in files]
        current = select or (Path(self.logo.get()) if self.logo.get() else None)
        if current and current.exists() and current.parent == self.app.paths.logos:
            self.logo_combo.set(current.name)
        elif not self.logo.get():
            self.logo_combo.set("Predefinito")

    def _select_logo(self, _event=None) -> None:
        name = self.logo_combo.get().strip()
        if name and name != "Predefinito":
            self.logo.set(str(self.app.paths.logos / name))

    def add_logo(self) -> None:
        selected = filedialog.askopenfilename(parent=self, title="Aggiungi un logo", filetypes=[("Immagini", "*.png *.jpg *.jpeg")])
        if not selected:
            return
        source = Path(selected)
        try:
            validate_logo_image(source)
        except LogoValidationError as exc:
            messagebox.showerror("Logo non valido", str(exc), parent=self)
            return

        target = self.app.paths.logos / source.name
        counter = 2
        while target.exists() and target.resolve() != source.resolve():
            target = self.app.paths.logos / f"{source.stem}_{counter}{source.suffix.lower()}"
            counter += 1
        if target.resolve() != source.resolve():
            shutil.copy2(source, target)
        self.logo.set(str(target))
        self._refresh_logos(target)

    def use_default(self) -> None:
        self.logo.set("")
        self.logo_combo.set("Predefinito")

    def save(self) -> None:
        try:
            retention_days = int(self.print_retention_days.get())
            if not 0 <= retention_days <= 3650:
                raise ValueError
        except (TypeError, ValueError, tk.TclError):
            messagebox.showerror(
                "Impostazioni",
                "La conservazione PDF deve essere compresa tra 0 e 3650 giorni.",
                parent=self,
            )
            return

        old_theme = self.app.settings.get("ui_theme", "system")
        old_retention = int(
            self.app.settings.get("print_retention_days", DEFAULT_PRINT_RETENTION_DAYS)
        )
        self.app.settings = self.app.settings_store.update(
            structure_type=self.structure_type.get(),
            structure_name=self.structure_name.get().strip(),
            wifi_title=self.wifi_title.get().strip(),
            preset=self.preset.get(),
            logo_path=self.logo.get().strip(),
            ui_theme=self.theme.get(),
            print_retention_days=retention_days,
        )
        if old_theme != self.theme.get():
            self.app.apply_theme()
        if old_retention != retention_days:
            self.app._cleanup_print_archive()
        self.destroy()


class HistoryRecoveryDialog(tk.Toplevel):
    """Inspect and explicitly recover the portable print-history identity."""

    def __init__(self, app: "ModernVoucherApp"):
        super().__init__(app)
        self.app = app
        self.title("Identità cronologia")
        self.transient(app)
        self.grab_set()
        self.confirmation = tk.StringVar()

        shell = ttk.Frame(self, padding=20)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text="Identità cronologia di stampa",
            style="PageTitle.TLabel",
        ).pack(anchor="w")
        self.state = app.history.identity_state()
        self._build_state(shell)
        fit_dialog(self, app, min_width=680, min_height=360)

    @staticmethod
    def _fingerprint(value: str) -> str:
        return format_fingerprint(value)

    def _build_state(self, shell: ttk.Frame) -> None:
        state = self.state
        if state.ready:
            ttk.Label(
                shell,
                text="La cronologia è disponibile e la chiave locale corrisponde "
                "al fingerprint registrato.",
                style="Muted.TLabel",
                wraplength=620,
            ).pack(anchor="w", pady=(8, 16))
            ttk.Label(
                shell,
                text=f"Fingerprint: {self._fingerprint(state.local_fingerprint)}",
            ).pack(anchor="w")
            ttk.Button(
                shell,
                text="Chiudi",
                command=self.destroy,
            ).pack(anchor="e", pady=(22, 0))
            return

        if state.reason == "missing_fingerprint" and state.can_adopt_local_key:
            ttk.Label(
                shell,
                text=(
                    "Esiste una cronologia con una chiave portabile locale, ma "
                    "manca il fingerprint che la associa ai record esistenti. "
                    "Adottare la chiave presente solo se questa cartella dati "
                    "proviene dalla stessa installazione o da un backup completo."
                ),
                style="Muted.TLabel",
                wraplength=620,
            ).pack(anchor="w", pady=(8, 16))
            fingerprint = self._fingerprint(state.local_fingerprint)
            ttk.Label(
                shell,
                text=f"Fingerprint della chiave presente: {fingerprint}",
            ).pack(anchor="w")
            ttk.Label(
                shell,
                text=(
                    "Per confermare, digitare esattamente il fingerprint "
                    "visualizzato sopra:"
                ),
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(16, 5))
            ttk.Entry(
                shell,
                textvariable=self.confirmation,
                width=42,
            ).pack(anchor="w")
            actions = ttk.Frame(shell)
            actions.pack(fill="x", pady=(22, 0))
            ttk.Button(
                actions,
                text="Annulla",
                command=self.destroy,
            ).pack(side="right")
            ttk.Button(
                actions,
                text="Adotta chiave presente",
                command=self._adopt,
                style="Accent.TButton",
            ).pack(side="right", padx=(0, 8))
            return

        expected = self._fingerprint(state.expected_fingerprint)
        local = self._fingerprint(state.local_fingerprint)
        if state.reason == "missing_local_key":
            detail = (
                "La chiave portabile della cronologia non è presente. "
                "Ripristinare un backup completo che contenga "
                "data/history_secret.key."
            )
        elif state.reason == "key_mismatch":
            detail = (
                "La chiave locale non corrisponde al fingerprint già associato "
                "alla cronologia. Per sicurezza non può essere adottata al posto "
                "di quella attesa. Ripristinare la chiave corretta da un backup."
            )
        else:
            detail = (
                "L'identità della cronologia non può essere recuperata "
                "automaticamente."
            )
        ttk.Label(
            shell,
            text=detail,
            style="Muted.TLabel",
            wraplength=620,
        ).pack(anchor="w", pady=(8, 16))
        if expected:
            ttk.Label(
                shell,
                text=f"Fingerprint atteso: {expected}",
            ).pack(anchor="w", pady=(0, 4))
        if local:
            ttk.Label(
                shell,
                text=f"Fingerprint chiave presente: {local}",
            ).pack(anchor="w")
        ttk.Button(
            shell,
            text="Chiudi",
            command=self.destroy,
        ).pack(anchor="e", pady=(22, 0))

    def _adopt(self) -> None:
        try:
            self.app.history.adopt_present_secret(
                self.confirmation.get()
            )
        except (HistoryError, ValueError) as exc:
            messagebox.showerror(
                "Identità cronologia",
                str(exc),
                parent=self,
            )
            return

        self.app.settings = self.app.settings_store.load()
        self.app._history_error_shown = False
        messagebox.showinfo(
            "Identità cronologia",
            "La chiave locale è stata associata alla cronologia esistente.",
            parent=self,
        )
        self.destroy()
        self.app.populate()


class ModernVoucherApp(DataMaintenanceMixin, ControllerConnectionMixin, VoucherDeletionMixin, VoucherApp):
    """Windows 11 operator shell around the stable voucher engine."""

    def _build_ui(self) -> None:
        self.apply_theme()
        self._configure_style()
        self.after_idle(self._maximize_window)
        root = ttk.Frame(self, padding=(22, 16))
        root.pack(fill="both", expand=True)
        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text=PRODUCT_NAME, style="PageTitle.TLabel").pack(side="left")
        ttk.Label(header, text="Voucher Wi-Fi", style="Muted.TLabel").pack(side="left", padx=(12, 0), pady=(7, 0))
        ttk.Button(header, text="Impostazioni", command=lambda: SettingsDialog(self)).pack(side="right")

        connection = ttk.Frame(root, padding=(14, 10))
        connection.pack(fill="x", pady=(0, 10))

        ttk.Label(connection, text="API root / Controller", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.api_root_entry = ttk.Entry(
            connection,
            textvariable=self.api_root_var,
            width=46,
        )
        self.api_root_entry.grid(row=0, column=1, sticky="ew", padx=(0, 16))
        self.api_root_entry.bind("<Return>", lambda _event: self.connect())

        ttk.Label(connection, text="API key", style="Muted.TLabel").grid(
            row=0, column=2, sticky="w", padx=(0, 6)
        )
        self.api_key_entry = ttk.Entry(
            connection,
            textvariable=self.api_key_var,
            show="•",
            width=32,
        )
        self.api_key_entry.grid(row=0, column=3, sticky="ew", padx=(0, 16))
        self.api_key_entry.bind("<Return>", lambda _event: self.connect())

        self.connect_button = ttk.Button(
            connection,
            text="Connetti",
            command=self.connect,
            style="Accent.TButton",
        )
        self.connect_button.grid(row=0, column=4)

        ttk.Label(
            connection,
            textvariable=self.connection_var,
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(
            connection,
            text="I certificati TLS non attendibili richiedono conferma esplicita dell'impronta SHA-256.",
            style="Muted.TLabel",
        ).grid(row=1, column=2, columnspan=3, sticky="w", pady=(8, 0))

        self.background_operation_var = tk.StringVar()
        self.background_progress = ttk.Progressbar(
            connection,
            mode="indeterminate",
        )
        self.background_progress.grid(
            row=2,
            column=0,
            columnspan=5,
            sticky="ew",
            pady=(9, 0),
        )
        self.background_progress.grid_remove()
        self.background_operation_label = ttk.Label(
            connection,
            textvariable=self.background_operation_var,
            style="Muted.TLabel",
        )
        self.background_operation_label.grid(
            row=3,
            column=0,
            columnspan=5,
            sticky="w",
            pady=(3, 0),
        )
        self.background_operation_label.grid_remove()

        connection.columnconfigure(1, weight=1)
        connection.columnconfigure(3, weight=1)

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(2, 12))
        self.create_button = ttk.Button(
            actions,
            text="＋  NUOVO VOUCHER",
            command=self.create,
            style="Hero.TButton",
        )
        self.create_button.pack(side="left")
        self.print_button = ttk.Button(
            actions,
            textvariable=self.action_var,
            command=self.print_selected,
            style="Hero.TButton",
        )
        self.print_button.pack(side="left", padx=(10, 22))
        ttk.Button(
            actions,
            text="Seleziona da stampare",
            command=self.select_unprinted,
        ).pack(side="left")
        self.refresh_button = ttk.Button(
            actions,
            text="Aggiorna",
            command=self.refresh,
        )
        self.refresh_button.pack(side="left", padx=8)
        self.open_pdf_button = ttk.Button(
            actions,
            text="Apri PDF",
            command=self.open_existing_pdf,
        )
        self.open_pdf_button.pack(side="left")
        self.delete_button = ttk.Button(
            actions,
            text="Elimina",
            command=self.delete_selected,
        )
        self.delete_button.pack(side="right")

        content = ttk.Frame(root)
        content.pack(fill="both", expand=True)
        nav = ttk.Frame(content)
        nav.pack(fill="x", pady=(0, 9))
        ttk.Label(nav, text="Voucher", style="SectionTitle.TLabel").pack(side="left", padx=(0, 14))
        cb = ttk.Combobox(nav, textvariable=self.filter_var, state="readonly", values=("Da stampare", "Attivi", "Scaduti", "Tutti"), width=14)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self.populate())
        ttk.Label(nav, text="Cerca", style="Muted.TLabel").pack(side="left", padx=(20, 7))
        search = ttk.Entry(nav, textvariable=self.search_var, width=34)
        search.pack(side="left")
        self._search_after = None
        search.bind("<KeyRelease>", self._schedule_search_populate)
        ttk.Label(nav, textvariable=self.count_var, style="Muted.TLabel").pack(side="right")

        table = ttk.Frame(content)
        table.pack(fill="both", expand=True)
        cols = ("check", "code", "name", "created", "firstprint", "duration", "usage", "printstatus", "copies", "expires")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("check", text="☐", command=self.toggle_all_visible)
        self.tree.column("check", width=38, anchor="center", stretch=False)
        definitions = (
            ("code", "Voucher", 110, False), ("name", "Destinatario", 220, True),
            ("created", "Creazione", 132, False), ("firstprint", "Prima stampa", 132, False),
            ("duration", "Durata", 78, False), ("usage", "Utilizzi", 86, False),
            ("printstatus", "Stato stampa", 118, False), ("copies", "Copie", 65, False),
            ("expires", "Scadenza", 132, False),
        )
        for key, label, width, stretch in definitions:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=60, anchor="w" if key == "name" else "center", stretch=stretch)
        self.tree.tag_configure("unprinted", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("pdfready", font=("Segoe UI", 10, "bold"))
        self.tree.bind("<Button-1>", self.on_tree_click)
        sy = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        ttk.Label(content, text="Elimina è disponibile solo prima della prima stampa. Un voucher stampato resta gestibile dall'amministratore UniFi.", style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

    def _set_background_busy(self, busy: bool, label: str = "") -> None:
        """Expose one serialized background operation in the main UI."""

        state = ["disabled"] if busy else ["!disabled"]
        for widget in (
            self.connect_button,
            self.create_button,
            self.refresh_button,
            self.delete_button,
            self.print_button,
            self.open_pdf_button,
        ):
            widget.state(state)

        if busy:
            self.background_operation_var.set(label)
            self.background_progress.grid()
            self.background_operation_label.grid()
            self.background_progress.start(12)
        else:
            self.background_progress.stop()
            self.background_progress.grid_remove()
            self.background_operation_label.grid_remove()
            self.background_operation_var.set("")

    def _set_network_busy(self, busy: bool, label: str = "") -> None:
        """Backward-compatible alias for the generalized busy indicator."""

        self._set_background_busy(busy, label)

    def _maximize_window(self) -> None:
        try:
            self.state("zoomed")
        except tk.TclError:
            self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")

    def apply_theme(self) -> None:
        """Apply Sun Valley light/dark appearance, following Windows by default."""
        preference = self.settings.get("ui_theme", "system")
        theme = ("dark" if darkdetect.isDark() else "light") if preference == "system" else preference
        sv_ttk.set_theme(theme if theme in {"light", "dark"} else "light")

    def _configure_style(self) -> None:
        """Add hierarchy on top of Sun Valley without replacing its palette."""
        style = ttk.Style(self)
        style.configure("PageTitle.TLabel", font=("Segoe UI Variable Display", 20, "bold"))
        style.configure("SectionTitle.TLabel", font=("Segoe UI Variable Text", 11, "bold"))
        style.configure("Muted.TLabel", font=("Segoe UI Variable Text", 9))
        style.configure("Hero.TButton", font=("Segoe UI Variable Text", 10, "bold"), padding=(20, 11))
        style.configure("Treeview", rowheight=36, font=("Segoe UI Variable Text", 10))
        style.configure("Treeview.Heading", font=("Segoe UI Variable Text", 9, "bold"), padding=(7, 9))
        self.minsize(1180, 700)

    def _history_stats_for(self, vouchers):
        """Return audit stats or block unsafe actions when history is unavailable."""
        try:
            stats = self.history.stats_for_codes(
                [v.code_formatted for v in vouchers],
                self.settings,
            )
        except HistoryError as exc:
            self.checked_ids.clear()
            self.action_var.set("PREPARA STAMPA")
            if not getattr(self, "_history_error_shown", False):
                messagebox.showerror(
                    "Cronologia non disponibile",
                    f"{exc}\n\n"
                    "Selezione, stampa ed eliminazione vengono sospese "
                    "per evitare decisioni basate su dati incompleti.\n\n"
                    "Aprire Impostazioni > Aspetto e dati > "
                    "Verifica / recupera identità per diagnosticare o "
                    "recuperare la chiave della cronologia.",
                    parent=self,
                )
                self._history_error_shown = True
            return None

        self._history_error_shown = False
        return stats

    def _schedule_search_populate(self, _event=None) -> None:
        """Debounce search refreshes to avoid rereading history per keystroke."""

        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except tk.TclError:
                pass
        self._search_after = self.after(220, self._run_search_populate)

    def _run_search_populate(self) -> None:
        self._search_after = None
        self.populate()

    def populate(self) -> None:
        """Render vouchers newest-first using UniFi creation time as reference."""
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.by_iid = {}
        filt, query = self.filter_var.get(), self.search_var.get().strip().lower()
        stats = self._history_stats_for(self.vouchers)
        if stats is None:
            self.count_var.set("Cronologia non disponibile  •  0 selezionati")
            return
        valid_ids = {v.id for v in self.vouchers if not self._is_expired(v)}
        self.checked_ids.intersection_update(valid_ids)
        candidates = []
        for voucher in self.vouchers:
            expired = self._is_expired(voucher)
            stat = stats.get(voucher.code_formatted)
            state = "SCADUTO" if expired else self._print_state(stat)
            if filt == "Da stampare" and (expired or state == "STAMPATO"):
                continue
            if filt == "Attivi" and expired:
                continue
            if filt == "Scaduti" and not expired:
                continue
            if query and query not in f"{voucher.recipient} {voucher.code_formatted}".lower():
                continue
            candidates.append((-voucher.create_time, voucher, stat, state))
        for _created, voucher, stat, state in sorted(candidates, key=lambda row: row[0]):
            expired = state == "SCADUTO"
            mark = "—" if expired else ("☑" if voucher.id in self.checked_ids else "☐")
            values = (mark, voucher.code_formatted, voucher.recipient or "-", time_label(voucher.create_time), audit_time_label(stat.first_print_utc if stat else ""), duration_label(voucher.duration_minutes), voucher.usage_label, state, stat.printed_copies if stat else 0, time_label(voucher.end_time))
            tags = ("expired",) if expired else (("unprinted",) if state == "DA STAMPARE" else (("pdfready",) if state == "PDF CREATO" else ()))
            iid = self.tree.insert("", "end", values=values, tags=tags)
            self.by_iid[iid] = voucher
        self.count_var.set(f"{len(candidates)} visualizzati  •  {len(self.checked_ids)} selezionati")
        self.action_var.set(f"PREPARA STAMPA  ({len(self.checked_ids)})" if self.checked_ids else "PREPARA STAMPA")

    def select_unprinted(self) -> None:
        """Select every active voucher that has not yet recorded a print job."""
        stats = self._history_stats_for(self.vouchers)
        if stats is None:
            return
        self.checked_ids = {v.id for v in self.vouchers if not self._is_expired(v) and self._print_state(stats.get(v.code_formatted)) in {"DA STAMPARE", "PDF CREATO"}}
        self.filter_var.set("Da stampare")
        self.populate()



def main() -> int:
    logging.getLogger("PIL").setLevel(logging.WARNING)
    try:
        app = ModernVoucherApp()
        if app.winfo_exists():
            app.mainloop()
        return 0
    except Exception as exc:
        logging.getLogger("voucher_management").error(
            "startup_failed type=%s",
            type(exc).__name__,
        )
        try:
            messagebox.showerror(
                "Avvio impossibile",
                "Voucher Management non è riuscito ad avviarsi. "
                "Controllare le impostazioni locali o ripristinare un backup "
                "valido, quindi riprovare.",
            )
        except Exception as dialog_exc:
            logging.getLogger("voucher_management").debug(
                "startup_error_dialog_failed type=%s",
                type(dialog_exc).__name__,
            )
        return 1
