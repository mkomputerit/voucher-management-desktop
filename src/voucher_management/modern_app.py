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
from .onboarding import OnboardingState
from .onboarding_ui import schedule_first_run_onboarding, startup_onboarding_state
from .pdf_render import VOUCHERS_PER_PAGE
from .print_archive import DEFAULT_PRINT_RETENTION_DAYS
from .report_ui import ReportDialog
from .reporting import ReportKind, build_report_dataset
from .retention_ui import RetentionMixin
from .utils import format_fingerprint
from .data_maintenance_ui import DataMaintenanceMixin
from .controller_connection_ui import ControllerConnectionMixin
from .voucher_deletion_ui import VoucherDeletionMixin
from .workspace_state import build_controller_workspace_status


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
        self.backup_on_close = tk.BooleanVar(
            value=bool(s.get("backup_on_close", True))
        )

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
        ttk.Label(
            frame,
            text="Conservazione voucher",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "I voucher usati o stampati sono sempre protetti. I voucher "
                "mai usati e mai stampati vengono proposti per la minimizzazione "
                "solo quando non sono più presenti sul controller e superano "
                "la soglia configurata. Nessuna pulizia è automatica."
            ),
            style="Muted.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(3, 10))
        ttk.Button(
            frame,
            text="Rivedi conservazione…",
            command=lambda: self.app.open_retention_review(parent=self),
        ).pack(anchor="w")

        ttk.Separator(frame).pack(fill="x", pady=22)
        ttk.Label(frame, text="Backup e ripristino", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "Il backup comprende configurazione, storico, PDF generati, "
                "loghi e la chiave portabile della cronologia. La API key UniFi "
                "non viene mai salvata. I nuovi backup creati dall'interfaccia "
                "sono sempre cifrati e autenticati."
            ),
            style="Muted.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(3, 10))
        ttk.Checkbutton(
            frame,
            text="Crea un backup cifrato prima della chiusura (consigliato)",
            variable=self.backup_on_close,
        ).pack(anchor="w", pady=(0, 10))
        ttk.Label(
            frame,
            text=(
                "Il backup automatico richiede la password alla chiusura e la "
                "password non viene memorizzata."
            ),
            style="Muted.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(0, 10))
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
            backup_on_close=bool(self.backup_on_close.get()),
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


class MigrationRequiredDialog(tk.Toplevel):
    """Block normal operation until explicit shared-data migration is resolved."""

    def __init__(self, app: "ModernVoucherApp"):
        super().__init__(app)
        self.app = app
        self.title("Migrazione dati richiesta")
        self.transient(app)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._close_application)

        shell = ttk.Frame(self, padding=22)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text="Dati precedenti rilevati",
            style="PageTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "Questa installazione condivisa ha rilevato dati della "
                "precedente installazione nel profilo Windows corrente. "
                "Per evitare sovrascritture, Voucher Management non abilita "
                "le normali operazioni finché la migrazione non viene "
                "completata o l'applicazione viene chiusa."
            ),
            style="Muted.TLabel",
            wraplength=620,
        ).pack(anchor="w", pady=(6, 18))

        actions = ttk.Frame(shell)
        actions.pack(fill="x")
        ttk.Button(
            actions,
            text="Chiudi",
            command=self._close_application,
        ).pack(side="right")
        ttk.Button(
            actions,
            text="Migra dati…",
            command=lambda: app.migrate_per_user_data_to_shared(parent=self),
            style="Accent.TButton",
        ).pack(side="right", padx=(0, 8))
        fit_dialog(self, app, min_width=680, min_height=260)

    def _close_application(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.app._finish_close(
            close_status="MIGRATION_DEFERRED",
            backup_status="NOT_REQUIRED",
        )


class ModernVoucherApp(
    RetentionMixin,
    DataMaintenanceMixin,
    ControllerConnectionMixin,
    VoucherDeletionMixin,
    VoucherApp,
):
    """Windows 11 operator shell around the stable voucher engine."""

    def _retention_intro_allowed_on_startup(self) -> bool:
        """Only legacy/existing installs need the separate retention intro."""

        return (
            startup_onboarding_state(self)
            is OnboardingState.EXISTING_INSTALLATION
        )

    def __init__(self):
        super().__init__()
        if not self.winfo_exists():
            return
        state = schedule_first_run_onboarding(self)
        if state is OnboardingState.MIGRATION_AVAILABLE:
            self.logger.info("onboarding_deferred legacy_migration_available=true")
            self.after_idle(self._show_legacy_migration_available)
        elif state is OnboardingState.EXISTING_INSTALLATION:
            self.logger.info(
                "onboarding_skipped existing_installation_without_profile=true"
            )

    def _show_legacy_migration_available(self) -> None:
        MigrationRequiredDialog(self)

    def _build_ui(self) -> None:
        """Build the operator-first 5.1 workspace without changing engine logic."""

        self.apply_theme()
        self._configure_style()
        self.after_idle(self._maximize_window)

        self._controller_status_failed = False
        self._controller_busy_label = ""
        self.workspace_title_var = tk.StringVar(value="Home")
        self.workspace_subtitle_var = tk.StringVar(
            value="Panoramica operativa della postazione"
        )
        self.controller_health_var = tk.StringVar()
        self.controller_health_detail_var = tk.StringVar()
        self.home_ready_var = tk.StringVar(value="Pronto")
        self.home_to_print_var = tk.StringVar(value="0")
        self.home_active_var = tk.StringVar(value="0")
        self.home_used_var = tk.StringVar(value="0")
        self.home_expired_var = tk.StringVar(value="0")
        self.report_total_var = tk.StringVar(value="0")
        self.report_printed_var = tk.StringVar(value="0")
        self.report_used_var = tk.StringVar(value="0")
        self.report_expired_var = tk.StringVar(value="0")

        root = ttk.Frame(self, padding=0)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(root, padding=(16, 20))
        sidebar.grid(row=0, column=0, sticky="ns")
        ttk.Label(
            sidebar,
            text=PRODUCT_NAME,
            style="Brand.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            sidebar,
            text="Gestione voucher Wi-Fi",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 24))

        self._nav_buttons = {}
        for key, label in (
            ("home", "Home"),
            ("voucher", "Voucher"),
            ("report", "Report"),
            ("settings", "Impostazioni"),
        ):
            button = ttk.Button(
                sidebar,
                text=label,
                style="Nav.TButton",
                command=lambda target=key: self._show_workspace(target),
                width=22,
            )
            button.pack(fill="x", pady=3)
            self._nav_buttons[key] = button

        ttk.Separator(sidebar).pack(fill="x", pady=(20, 14))
        ttk.Label(
            sidebar,
            textvariable=self.controller_health_var,
            style="Status.TLabel",
            wraplength=190,
        ).pack(anchor="w")
        ttk.Label(
            sidebar,
            textvariable=self.controller_health_detail_var,
            style="Muted.TLabel",
            wraplength=190,
        ).pack(anchor="w", pady=(4, 0))

        main = ttk.Frame(root, padding=(26, 18, 26, 22))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        title_box = ttk.Frame(header)
        title_box.pack(side="left", fill="x", expand=True)
        ttk.Label(
            title_box,
            textvariable=self.workspace_title_var,
            style="PageTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            title_box,
            textvariable=self.workspace_subtitle_var,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        self.background_operation_var = tk.StringVar()
        progress_box = ttk.Frame(main)
        progress_box.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        progress_box.columnconfigure(0, weight=1)
        self.background_progress = ttk.Progressbar(
            progress_box,
            mode="indeterminate",
        )
        self.background_progress.grid(row=0, column=0, sticky="ew")
        self.background_progress.grid_remove()
        self.background_operation_label = ttk.Label(
            progress_box,
            textvariable=self.background_operation_var,
            style="Muted.TLabel",
        )
        self.background_operation_label.grid(
            row=1,
            column=0,
            sticky="w",
            pady=(3, 0),
        )
        self.background_operation_label.grid_remove()

        self.page_host = ttk.Frame(main)
        self.page_host.grid(row=2, column=0, sticky="nsew")
        self.page_host.columnconfigure(0, weight=1)
        self.page_host.rowconfigure(0, weight=1)
        self._workspace_pages = {}
        for key in ("home", "voucher", "report", "settings"):
            frame = ttk.Frame(self.page_host)
            frame.grid(row=0, column=0, sticky="nsew")
            self._workspace_pages[key] = frame

        self._build_home_workspace(self._workspace_pages["home"])
        self._build_voucher_workspace(self._workspace_pages["voucher"])
        self._build_report_workspace(self._workspace_pages["report"])
        self._build_settings_workspace(self._workspace_pages["settings"])

        self._busy_widgets = [
            self.connect_button,
            self.create_button,
            self.home_create_button,
            self.home_sync_button,
            self.refresh_button,
            self.delete_button,
            self.print_button,
            self.open_pdf_button,
            self.report_button,
        ]
        self._search_after = None
        self._show_workspace("home")
        self._refresh_controller_workspace_status()

    def _build_home_workspace(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(0, weight=1)

        hero = ttk.Frame(frame, padding=(0, 4, 0, 18))
        hero.grid(row=0, column=0, sticky="ew")
        hero.columnconfigure(0, weight=1)
        ttk.Label(
            hero,
            text="Postazione voucher",
            style="HeroTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            hero,
            text=(
                "Crea e stampa voucher senza dover gestire i dettagli tecnici "
                "del controller."
            ),
            style="Body.TLabel",
            wraplength=720,
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        quick = ttk.Frame(hero)
        quick.grid(row=0, column=1, rowspan=2, sticky="e")
        self.home_create_button = ttk.Button(
            quick,
            text="＋ Nuovo voucher",
            command=self.create,
            style="Hero.TButton",
        )
        self.home_create_button.pack(side="left")
        self.home_sync_button = ttk.Button(
            quick,
            text="Sincronizza",
            command=self.refresh,
        )
        self.home_sync_button.pack(side="left", padx=(10, 0))

        metrics = ttk.Frame(frame)
        metrics.grid(row=1, column=0, sticky="ew", pady=(0, 18))
        for column in range(4):
            metrics.columnconfigure(column, weight=1)
        for column, (label, variable) in enumerate((
            ("Da stampare", self.home_to_print_var),
            ("Attivi", self.home_active_var),
            ("Utilizzati", self.home_used_var),
            ("Scaduti", self.home_expired_var),
        )):
            card = ttk.Labelframe(metrics, text=label, padding=(16, 12))
            card.grid(
                row=0,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 6, 0 if column == 3 else 6),
            )
            ttk.Label(
                card,
                textvariable=variable,
                style="Metric.TLabel",
            ).pack(anchor="w")

        status = ttk.Labelframe(
            frame,
            text="Stato applicazione",
            padding=(18, 14),
        )
        status.grid(row=2, column=0, sticky="ew", pady=(0, 18))
        status.columnconfigure(0, weight=1)
        ttk.Label(
            status,
            textvariable=self.controller_health_var,
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            status,
            textvariable=self.controller_health_detail_var,
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Button(
            status,
            text="Apri impostazioni controller",
            command=lambda: self._show_workspace("settings"),
        ).grid(row=0, column=1, rowspan=2, sticky="e", padx=(18, 0))

        recent = ttk.Labelframe(
            frame,
            text="Attività recente",
            padding=(12, 10),
        )
        recent.grid(row=3, column=0, sticky="nsew")
        frame.rowconfigure(3, weight=1)
        recent.columnconfigure(0, weight=1)
        recent.rowconfigure(0, weight=1)
        self.home_recent_tree = ttk.Treeview(
            recent,
            columns=("recipient", "state", "created"),
            show="headings",
            height=6,
        )
        self.home_recent_tree.heading("recipient", text="Voucher / destinatario")
        self.home_recent_tree.heading("state", text="Stato")
        self.home_recent_tree.heading("created", text="Creazione")
        self.home_recent_tree.column("recipient", width=360, anchor="w")
        self.home_recent_tree.column("state", width=150, anchor="center")
        self.home_recent_tree.column("created", width=170, anchor="center")
        self.home_recent_tree.grid(row=0, column=0, sticky="nsew")

    def _build_voucher_workspace(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        actions = ttk.Frame(frame)
        actions.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self.create_button = ttk.Button(
            actions,
            text="＋ Nuovo voucher",
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
        self.print_button.pack(side="left", padx=(10, 18))
        ttk.Button(
            actions,
            text="Seleziona da stampare",
            command=self.select_unprinted,
        ).pack(side="left")
        self.refresh_button = ttk.Button(
            actions,
            text="Sincronizza",
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

        filters = ttk.Frame(frame)
        filters.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(
            filters,
            text="Vista",
            style="Muted.TLabel",
        ).pack(side="left", padx=(0, 7))
        cb = ttk.Combobox(
            filters,
            textvariable=self.filter_var,
            state="readonly",
            values=("Da stampare", "Attivi", "Scaduti", "Tutti"),
            width=15,
        )
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self.populate())
        ttk.Label(
            filters,
            text="Cerca",
            style="Muted.TLabel",
        ).pack(side="left", padx=(20, 7))
        search = ttk.Entry(
            filters,
            textvariable=self.search_var,
            width=34,
        )
        search.pack(side="left")
        search.bind("<KeyRelease>", self._schedule_search_populate)
        ttk.Label(
            filters,
            textvariable=self.count_var,
            style="Muted.TLabel",
        ).pack(side="right")

        table = ttk.Frame(frame)
        table.grid(row=2, column=0, sticky="nsew")
        cols = (
            "check",
            "code",
            "name",
            "created",
            "firstprint",
            "duration",
            "usage",
            "printstatus",
            "copies",
            "expires",
        )
        self.tree = ttk.Treeview(
            table,
            columns=cols,
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("check", text="✓", command=self.toggle_all_visible)
        self.tree.column("check", width=42, anchor="center", stretch=False)
        definitions = (
            ("code", "Voucher", 110, False),
            ("name", "Destinatario", 220, True),
            ("created", "Creazione", 132, False),
            ("firstprint", "Prima stampa", 132, False),
            ("duration", "Durata", 78, False),
            ("usage", "Utilizzi", 86, False),
            ("printstatus", "Stato stampa", 118, False),
            ("copies", "Copie", 65, False),
            ("expires", "Scadenza", 132, False),
        )
        for key, label, width, stretch in definitions:
            self.tree.heading(key, text=label)
            self.tree.column(
                key,
                width=width,
                minwidth=60,
                anchor="w" if key == "name" else "center",
                stretch=stretch,
            )
        self.tree.tag_configure(
            "unprinted",
            font=("Segoe UI Variable Text", 10, "bold"),
        )
        self.tree.tag_configure(
            "pdfready",
            font=("Segoe UI Variable Text", 10, "bold"),
        )
        self.tree.bind("<Button-1>", self.on_tree_click)
        sy = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        ttk.Label(
            frame,
            text=(
                "Clicca una riga per selezionarla. La ristampa resta una "
                "azione esplicita e richiede conferma quando esiste già una "
                "stampa fisica registrata."
            ),
            style="Muted.TLabel",
        ).grid(row=3, column=0, sticky="w", pady=(8, 0))

    def _build_report_workspace(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(0, weight=1)
        ttk.Label(
            frame,
            text="Riepilogo amministrativo",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text=(
                "Consulta i principali indicatori e genera PDF o CSV senza "
                "esporre i codici voucher in chiaro nei report ordinari."
            ),
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=1, column=0, sticky="w", pady=(3, 16))

        metrics = ttk.Frame(frame)
        metrics.grid(row=2, column=0, sticky="ew")
        for column in range(4):
            metrics.columnconfigure(column, weight=1)
        for column, (label, variable) in enumerate((
            ("Voucher", self.report_total_var),
            ("Stampati", self.report_printed_var),
            ("Utilizzati", self.report_used_var),
            ("Scaduti", self.report_expired_var),
        )):
            card = ttk.Labelframe(metrics, text=label, padding=(16, 12))
            card.grid(
                row=0,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 6, 0 if column == 3 else 6),
            )
            ttk.Label(
                card,
                textvariable=variable,
                style="Metric.TLabel",
            ).pack(anchor="w")

        actions = ttk.Labelframe(
            frame,
            text="Esporta report",
            padding=(18, 14),
        )
        actions.grid(row=3, column=0, sticky="ew", pady=(20, 0))
        ttk.Label(
            actions,
            text=(
                "Scegli riepilogo, utilizzati, scaduti, stampati mai "
                "utilizzati, nominali o storico completo."
            ),
            style="Muted.TLabel",
            wraplength=680,
        ).pack(anchor="w", pady=(0, 12))
        self.report_button = ttk.Button(
            actions,
            text="Crea / esporta report…",
            command=lambda: ReportDialog(self),
            style="Accent.TButton",
        )
        self.report_button.pack(anchor="w")

    def _build_settings_workspace(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(0, weight=1)
        ttk.Label(
            frame,
            text="Configurazione della postazione",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text=(
                "Le impostazioni tecniche sono separate dalle attività "
                "quotidiane dell'operatore."
            ),
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(3, 16))

        controller = ttk.Labelframe(
            frame,
            text="Controller",
            padding=(18, 14),
        )
        controller.grid(row=2, column=0, sticky="ew")
        controller.columnconfigure(1, weight=1)
        ttk.Label(controller, text="Indirizzo controller").grid(
            row=0, column=0, sticky="w", padx=(0, 14), pady=6
        )
        self.api_root_entry = ttk.Entry(
            controller,
            textvariable=self.api_root_var,
        )
        self.api_root_entry.grid(row=0, column=1, sticky="ew", pady=6)
        self.api_root_entry.bind("<Return>", lambda _event: self.connect())
        ttk.Label(controller, text="API key").grid(
            row=1, column=0, sticky="w", padx=(0, 14), pady=6
        )
        self.api_key_entry = ttk.Entry(
            controller,
            textvariable=self.api_key_var,
            show="•",
        )
        self.api_key_entry.grid(row=1, column=1, sticky="ew", pady=6)
        self.api_key_entry.bind("<Return>", lambda _event: self.connect())
        self.connect_button = ttk.Button(
            controller,
            text="Verifica / connetti",
            command=self.connect,
            style="Accent.TButton",
        )
        self.connect_button.grid(row=0, column=2, rowspan=2, padx=(14, 0))
        ttk.Label(
            controller,
            textvariable=self.connection_var,
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(
            controller,
            text=(
                "La API key viene usata solo per la connessione corrente e "
                "non viene salvata."
            ),
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        general = ttk.Labelframe(
            frame,
            text="Generali e PDF / stampa",
            padding=(18, 14),
        )
        general.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(
            general,
            text=(
                "Identità della struttura, tema, logo, archivio PDF e "
                "preferenze di stampa."
            ),
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 10))
        ttk.Button(
            general,
            text="Apri preferenze…",
            command=lambda: SettingsDialog(self),
        ).pack(anchor="w")

        retention = ttk.Labelframe(
            frame,
            text="Retention",
            padding=(18, 14),
        )
        retention.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(
            retention,
            text=(
                "Rivedi in modo guidato quali voucher inutilizzati possono "
                "essere minimizzati. Nessuna pulizia è automatica."
            ),
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 10))
        ttk.Button(
            retention,
            text="Rivedi conservazione…",
            command=lambda: self.open_retention_review(parent=self),
        ).pack(anchor="w")

        backup = ttk.Labelframe(
            frame,
            text="Backup",
            padding=(18, 14),
        )
        backup.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(
            backup,
            text=(
                "Proteggi o ripristina la configurazione e lo storico della "
                "postazione."
            ),
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 10))
        backup_actions = ttk.Frame(backup)
        backup_actions.pack(anchor="w")
        ttk.Button(
            backup_actions,
            text="Crea backup…",
            command=lambda: self.create_backup(parent=self),
        ).pack(side="left")
        ttk.Button(
            backup_actions,
            text="Ripristina backup…",
            command=lambda: self.restore_backup(parent=self),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            backup_actions,
            text="Recupera identità cronologia…",
            command=lambda: HistoryRecoveryDialog(self),
        ).pack(side="left", padx=(8, 0))

    def _show_workspace(self, key: str) -> None:
        titles = {
            "home": (
                "Home",
                "Panoramica operativa della postazione",
            ),
            "voucher": (
                "Voucher",
                "Crea, seleziona, stampa e gestisci i voucher",
            ),
            "report": (
                "Report",
                "Riepiloghi ed esportazioni amministrative",
            ),
            "settings": (
                "Impostazioni",
                "Controller, PDF, retention e protezione dei dati",
            ),
        }
        if key not in self._workspace_pages:
            return
        self._workspace_pages[key].tkraise()
        self.workspace_title_var.set(titles[key][0])
        self.workspace_subtitle_var.set(titles[key][1])
        for name, button in self._nav_buttons.items():
            button.configure(
                style="NavActive.TButton" if name == key else "Nav.TButton"
            )
        if key == "report":
            self._refresh_report_summary()
        self._refresh_controller_workspace_status()

    def _controller_record(self):
        controller_id = getattr(self, "active_controller_id", None)
        if controller_id is None:
            return None
        return self.database.connection.execute(
            """SELECT name, last_successful_sync_at
               FROM controllers WHERE id=?""",
            (controller_id,),
        ).fetchone()

    def _refresh_controller_workspace_status(self) -> None:
        if not hasattr(self, "controller_health_var"):
            return
        row = self._controller_record()
        configured = bool(
            row is not None
            or str(self.api_root_var.get() or "").strip()
        )
        name = (
            str(row["name"] or "").strip()
            if row is not None
            else ""
        )
        last_sync = (
            str(row["last_successful_sync_at"] or "").strip()
            if row is not None
            else ""
        )
        status = build_controller_workspace_status(
            connected=self.client is not None,
            configured=configured,
            controller_name=name,
            last_successful_sync_at=last_sync,
            busy_label=self._controller_busy_label,
            failed=self._controller_status_failed,
        )
        self.controller_health_var.set(status.title)
        self.controller_health_detail_var.set(status.detail)
        self.home_ready_var.set(status.title)

    def _controller_operation_failed(self) -> None:
        self._controller_status_failed = True
        self._refresh_controller_workspace_status()

    def _controller_operation_succeeded(self) -> None:
        self._controller_status_failed = False
        self._refresh_controller_workspace_status()

    def _set_background_busy(self, busy: bool, label: str = "") -> None:
        """Expose one serialized operation without blocking the operator shell."""

        state = ["disabled"] if busy else ["!disabled"]
        widgets = getattr(self, "_busy_widgets", None)
        if widgets is None:
            widgets = tuple(
                widget
                for widget in (
                    getattr(self, "connect_button", None),
                    getattr(self, "create_button", None),
                    getattr(self, "refresh_button", None),
                    getattr(self, "delete_button", None),
                    getattr(self, "print_button", None),
                    getattr(self, "open_pdf_button", None),
                    getattr(self, "report_button", None),
                )
                if widget is not None
            )
        for widget in widgets:
            widget.state(state)

        network_words = ("connessione", "aggiornamento", "sincron")
        if busy and any(word in label.lower() for word in network_words):
            self._controller_busy_label = label
        elif not busy:
            self._controller_busy_label = ""

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
        refresh_status = getattr(
            self,
            "_refresh_controller_workspace_status",
            None,
        )
        if refresh_status is not None:
            refresh_status()

    def _set_network_busy(self, busy: bool, label: str = "") -> None:
        """Backward-compatible alias for the generalized busy indicator."""

        self._set_background_busy(busy, label)

    def _maximize_window(self) -> None:
        try:
            self.state("zoomed")
        except tk.TclError:
            self.geometry(
                f"{self.winfo_screenwidth()}x"
                f"{self.winfo_screenheight()}+0+0"
            )

    def apply_theme(self) -> None:
        """Apply Sun Valley light/dark appearance, following Windows by default."""

        preference = self.settings.get("ui_theme", "system")
        theme = (
            "dark" if darkdetect.isDark() else "light"
        ) if preference == "system" else preference
        sv_ttk.set_theme(theme if theme in {"light", "dark"} else "light")

    def _configure_style(self) -> None:
        """Build hierarchy on Sun Valley while retaining the Windows palette."""

        style = ttk.Style(self)
        style.configure(
            "Brand.TLabel",
            font=("Segoe UI Variable Display", 16, "bold"),
        )
        style.configure(
            "PageTitle.TLabel",
            font=("Segoe UI Variable Display", 22, "bold"),
        )
        style.configure(
            "HeroTitle.TLabel",
            font=("Segoe UI Variable Display", 18, "bold"),
        )
        style.configure(
            "SectionTitle.TLabel",
            font=("Segoe UI Variable Text", 11, "bold"),
        )
        style.configure(
            "Body.TLabel",
            font=("Segoe UI Variable Text", 10),
        )
        style.configure(
            "Muted.TLabel",
            font=("Segoe UI Variable Text", 9),
        )
        style.configure(
            "Status.TLabel",
            font=("Segoe UI Variable Text", 10, "bold"),
        )
        style.configure(
            "Metric.TLabel",
            font=("Segoe UI Variable Display", 24, "bold"),
        )
        style.configure(
            "Hero.TButton",
            font=("Segoe UI Variable Text", 10, "bold"),
            padding=(18, 10),
        )
        style.configure(
            "Nav.TButton",
            font=("Segoe UI Variable Text", 10),
            padding=(12, 10),
            anchor="w",
        )
        style.configure(
            "NavActive.TButton",
            font=("Segoe UI Variable Text", 10, "bold"),
            padding=(12, 10),
            anchor="w",
        )
        style.configure(
            "Treeview",
            rowheight=38,
            font=("Segoe UI Variable Text", 10),
        )
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI Variable Text", 9, "bold"),
            padding=(7, 9),
        )
        self.minsize(1180, 720)

    def _refresh_report_summary(self) -> None:
        if not hasattr(self, "report_total_var"):
            return
        try:
            dataset = build_report_dataset(
                self.database,
                kind=ReportKind.SUMMARY,
                generated_at=datetime.now().astimezone().isoformat(),
                controller_id=None,
            )
        except Exception as exc:
            self.logger.warning(
                "report_workspace_summary_failed type=%s",
                type(exc).__name__,
            )
            return
        totals = dataset.totals
        self.report_total_var.set(str(totals.vouchers))
        self.report_printed_var.set(str(totals.printed_vouchers))
        self.report_used_var.set(str(totals.used_vouchers))
        self.report_expired_var.set(str(totals.expired_vouchers))

    def _update_operator_summary(self, stats) -> None:
        if not hasattr(self, "home_to_print_var"):
            return
        active = 0
        expired_count = 0
        used = 0
        to_print = 0
        recent_rows = []
        for voucher in self.vouchers:
            expired = self._is_expired(voucher)
            stat = stats.get(voucher.code_formatted)
            state = "SCADUTO" if expired else self._print_state(stat)
            if expired:
                expired_count += 1
            else:
                active += 1
                if state != "STAMPATO":
                    to_print += 1
            if voucher.used > 0:
                used += 1
            recent_rows.append(
                (
                    -int(voucher.create_time or 0),
                    voucher.recipient or voucher.code_formatted,
                    state,
                    time_label(voucher.create_time),
                )
            )
        self.home_to_print_var.set(str(to_print))
        self.home_active_var.set(str(active))
        self.home_used_var.set(str(used))
        self.home_expired_var.set(str(expired_count))

        if hasattr(self, "home_recent_tree"):
            for iid in self.home_recent_tree.get_children():
                self.home_recent_tree.delete(iid)
            for _order, recipient, state, created in sorted(recent_rows)[:6]:
                self.home_recent_tree.insert(
                    "",
                    "end",
                    values=(recipient, state, created),
                )
        self._refresh_report_summary()
        self._refresh_controller_workspace_status()

    def _sync_selection_ui(self, iids=None) -> None:
        """Keep checkbox state and native row highlighting in lockstep."""

        super()._sync_selection_ui(iids)
        selected_iids = [
            iid
            for iid, voucher in self.by_iid.items()
            if voucher.id in self.checked_ids
        ]
        self.tree.selection_set(selected_iids)

    def on_tree_click(self, event):
        """Treat clicking the row as the print selection, not a second concept."""

        if self.tree.identify_region(event.x, event.y) != "cell":
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
        self._sync_selection_ui((iid,))
        return "break"

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
                logging.getLogger("voucher_management").debug(
                    "search_after_cancel_ignored"
                )
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
            self._refresh_controller_workspace_status()
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
        self.count_var.set(
            f"{len(candidates)} visualizzati  •  "
            f"{len(self.checked_ids)} selezionati"
        )
        self.action_var.set(
            f"PREPARA STAMPA  ({len(self.checked_ids)})"
            if self.checked_ids
            else "PREPARA STAMPA"
        )
        self._update_operator_summary(stats)
        self._sync_selection_ui()

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
