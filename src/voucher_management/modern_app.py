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
from .onboarding import OnboardingState, choose_shared_fresh_start
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
from .workspace_overview import load_recent_workspace_activity


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
                "Puoi migrarli, ripristinare un backup oppure iniziare una "
                "nuova installazione separata. I dati precedenti non vengono "
                "mai cancellati automaticamente."
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
            text="Inizia nuova installazione",
            command=self._start_fresh_installation,
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            actions,
            text="Ripristina backup…",
            command=lambda: app.restore_backup(parent=self),
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            actions,
            text="Migra dati precedenti…",
            command=lambda: app.migrate_per_user_data_to_shared(parent=self),
            style="Accent.TButton",
        ).pack(side="right", padx=(0, 8))
        fit_dialog(self, app, min_width=860, min_height=290)

    def _start_fresh_installation(self) -> None:
        if not messagebox.askyesno(
            "Nuova installazione",
            "Iniziare una nuova installazione condivisa senza importare i "
            "dati trovati nel profilo Windows corrente?\n\n"
            "I dati precedenti resteranno invariati e potranno essere "
            "recuperati manualmente in seguito.",
            parent=self,
        ):
            return
        try:
            choose_shared_fresh_start(self.app.database)
        except Exception as exc:
            self.app.logger.error(
                "shared_fresh_start_marker_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Nuova installazione",
                "Impossibile registrare in sicurezza la scelta. "
                "Nessun dato è stato modificato.",
                parent=self,
            )
            return
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self.app.after_idle(lambda: FirstRunWizard(self.app))

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
        """Build the operator-first workspace around the stable 5.x engine."""

        self.apply_theme()
        self._configure_style()
        self.after_idle(self._maximize_window)

        self._controller_status_failed = False
        self._controller_busy_label = ""
        self.workspace_title_var = tk.StringVar(value="Home")
        self.workspace_subtitle_var = tk.StringVar(
            value="Panoramica generale e accesso rapido alle funzioni principali"
        )
        profile = self.database.installation_profile()
        installation_label = (
            str(profile["installation_name"] or "").strip()
            if profile is not None
            else ""
        )
        self.installation_display_var = tk.StringVar(
            value=(
                installation_label
                or str(self.settings.get("structure_name", "") or "").strip()
                or PRODUCT_NAME
            )
        )
        self.controller_health_var = tk.StringVar()
        self.controller_health_detail_var = tk.StringVar()
        self.sidebar_sync_var = tk.StringVar(value="Ultimo aggiornamento\n—")
        self.home_controller_name_var = tk.StringVar(value="Nessun controller")
        self.home_last_sync_var = tk.StringVar(value="Mai")
        self.home_sync_detail_var = tk.StringVar()
        self.home_ready_var = tk.StringVar(value="Controller da configurare")
        self.home_sync_action_var = tk.StringVar(value="Configura controller")
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

        sidebar = ttk.Frame(
            root,
            style="Sidebar.TFrame",
            padding=(18, 22),
            width=232,
        )
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(2, weight=1)

        brand = ttk.Frame(sidebar, style="Sidebar.TFrame")
        brand.grid(row=0, column=0, sticky="ew", pady=(0, 22))
        ttk.Label(
            brand,
            text=PRODUCT_NAME,
            style="SidebarBrand.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            brand,
            text="Gestione voucher Wi-Fi",
            style="SidebarMuted.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        nav = ttk.Frame(sidebar, style="Sidebar.TFrame")
        nav.grid(row=1, column=0, sticky="new")
        self._nav_buttons = {}
        for key, label in (
            ("home", "⌂   Home"),
            ("voucher", "▣   Voucher"),
            ("report", "▤   Report"),
            ("settings", "⚙   Impostazioni"),
        ):
            button = ttk.Button(
                nav,
                text=label,
                style="Nav.TButton",
                command=lambda target=key: self._show_workspace(target),
                width=22,
            )
            button.pack(fill="x", pady=3)
            self._nav_buttons[key] = button

        status_box = ttk.Frame(sidebar, style="Sidebar.TFrame")
        status_box.grid(row=3, column=0, sticky="sew")
        ttk.Separator(status_box).pack(fill="x", pady=(0, 14))
        ttk.Label(
            status_box,
            text="Stato applicazione",
            style="SidebarMuted.TLabel",
        ).pack(anchor="w")
        self.sidebar_status_label = ttk.Label(
            status_box,
            textvariable=self.controller_health_var,
            style="SidebarStatus.TLabel",
            wraplength=190,
        )
        self.sidebar_status_label.pack(anchor="w", pady=(6, 2))
        ttk.Label(
            status_box,
            textvariable=self.sidebar_sync_var,
            style="SidebarMuted.TLabel",
            wraplength=190,
        ).pack(anchor="w", pady=(8, 10))
        self.sidebar_action_button = ttk.Button(
            status_box,
            textvariable=self.home_sync_action_var,
            command=self._home_sync_or_connect,
        )
        self.sidebar_action_button.pack(fill="x")

        main = ttk.Frame(root, padding=(28, 20, 28, 24))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
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
        ttk.Label(
            header,
            textvariable=self.installation_display_var,
            style="HeaderMeta.TLabel",
        ).pack(side="right", anchor="ne", padx=(20, 0), pady=(5, 0))

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
            self.sidebar_action_button,
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
        """Build the operator dashboard shown after startup."""

        frame.columnconfigure(0, weight=3)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.rowconfigure(2, weight=1)

        metrics = ttk.Frame(frame)
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 16))
        for column in range(4):
            metrics.columnconfigure(column, weight=1)
        for column, (label, variable, hint) in enumerate((
            ("Da stampare", self.home_to_print_var, "pronti per l'operatore"),
            ("Attivi", self.home_active_var, "voucher disponibili"),
            ("Utilizzati", self.home_used_var, "almeno un utilizzo"),
            ("Scaduti", self.home_expired_var, "nello storico locale"),
        )):
            card = ttk.Labelframe(
                metrics,
                text=label,
                style="Card.TLabelframe",
                padding=(16, 12),
            )
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
            ttk.Label(
                card,
                text=hint,
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(2, 0))

        recent = ttk.Labelframe(
            frame,
            text="Voucher recenti",
            style="Card.TLabelframe",
            padding=(12, 10),
        )
        recent.grid(row=1, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        recent.columnconfigure(0, weight=1)
        recent.rowconfigure(1, weight=1)
        recent_header = ttk.Frame(recent)
        recent_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(
            recent_header,
            text="Ultimi voucher disponibili nella postazione",
            style="Muted.TLabel",
        ).pack(side="left")
        self.home_create_button = ttk.Button(
            recent_header,
            text="＋ Nuovo voucher",
            command=self.create,
            style="Accent.TButton",
        )
        self.home_create_button.pack(side="right")
        self.home_recent_tree = ttk.Treeview(
            recent,
            columns=("code", "recipient", "state", "expires", "created"),
            show="headings",
            height=7,
        )
        for key, label, width, anchor in (
            ("code", "Voucher", 125, "center"),
            ("recipient", "Destinatario", 260, "w"),
            ("state", "Stato", 125, "center"),
            ("expires", "Scadenza", 145, "center"),
            ("created", "Creato", 145, "center"),
        ):
            self.home_recent_tree.heading(key, text=label)
            self.home_recent_tree.column(
                key,
                width=width,
                anchor=anchor,
                stretch=(key == "recipient"),
            )
        self.home_recent_tree.grid(row=1, column=0, sticky="nsew")

        controller = ttk.Labelframe(
            frame,
            text="Stato controller UniFi",
            style="Card.TLabelframe",
            padding=(18, 14),
        )
        controller.grid(row=1, column=1, sticky="nsew", pady=(0, 12))
        self.home_status_title_label = ttk.Label(
            controller,
            textvariable=self.home_ready_var,
            style="Status.TLabel",
        )
        self.home_status_title_label.pack(anchor="w")
        ttk.Label(
            controller,
            textvariable=self.home_sync_detail_var,
            style="Muted.TLabel",
            wraplength=300,
        ).pack(anchor="w", pady=(5, 14))
        ttk.Separator(controller).pack(fill="x", pady=(0, 12))
        ttk.Label(
            controller,
            text="Controller",
            style="Muted.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            controller,
            textvariable=self.home_controller_name_var,
            style="SectionTitle.TLabel",
            wraplength=300,
        ).pack(anchor="w", pady=(2, 10))
        ttk.Label(
            controller,
            text="Ultima sincronizzazione",
            style="Muted.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            controller,
            textvariable=self.home_last_sync_var,
            style="Body.TLabel",
        ).pack(anchor="w", pady=(2, 14))
        self.home_sync_button = ttk.Button(
            controller,
            textvariable=self.home_sync_action_var,
            command=self._home_sync_or_connect,
            style="Accent.TButton",
        )
        self.home_sync_button.pack(fill="x")

        activity = ttk.Labelframe(
            frame,
            text="Attività recenti",
            style="Card.TLabelframe",
            padding=(12, 10),
        )
        activity.grid(row=2, column=0, sticky="nsew", padx=(0, 12))
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(0, weight=1)
        self.home_activity_tree = ttk.Treeview(
            activity,
            columns=("time", "activity", "detail"),
            show="headings",
            height=6,
        )
        self.home_activity_tree.heading("time", text="Quando")
        self.home_activity_tree.heading("activity", text="Attività")
        self.home_activity_tree.heading("detail", text="Dettaglio")
        self.home_activity_tree.column("time", width=140, anchor="center", stretch=False)
        self.home_activity_tree.column("activity", width=190, anchor="w", stretch=False)
        self.home_activity_tree.column("detail", width=420, anchor="w", stretch=True)
        self.home_activity_tree.grid(row=0, column=0, sticky="nsew")

        quick = ttk.Labelframe(
            frame,
            text="Azioni rapide",
            style="Card.TLabelframe",
            padding=(14, 12),
        )
        quick.grid(row=2, column=1, sticky="nsew")
        for text, command, primary in (
            ("＋  Crea nuovo voucher", self.create, True),
            ("Vai ai voucher", lambda: self._show_workspace("voucher"), False),
            ("Apri report", lambda: self._show_workspace("report"), False),
            ("Vai alle impostazioni", lambda: self._show_workspace("settings"), False),
        ):
            ttk.Button(
                quick,
                text=text,
                command=command,
                style="Accent.TButton" if primary else "TButton",
            ).pack(fill="x", pady=(0, 8))

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
        """Build separated, operator-facing settings categories."""

        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.settings_structure_type_var = tk.StringVar()
        self.settings_structure_name_var = tk.StringVar()
        self.settings_wifi_title_var = tk.StringVar()
        self.settings_theme_var = tk.StringVar()
        self.settings_preset_var = tk.StringVar()
        self.settings_logo_var = tk.StringVar()
        self.settings_pdf_retention_var = tk.StringVar()
        self.settings_backup_on_close_var = tk.BooleanVar()
        self.settings_save_status_var = tk.StringVar()
        self.settings_backup_summary_var = tk.StringVar()
        self.settings_retention_summary_var = tk.StringVar()

        notebook = ttk.Notebook(frame)
        notebook.grid(row=0, column=0, sticky="nsew")
        self.settings_notebook = notebook

        general = ttk.Frame(notebook, padding=22)
        controller = ttk.Frame(notebook, padding=22)
        pdf_print = ttk.Frame(notebook, padding=22)
        retention = ttk.Frame(notebook, padding=22)
        backup = ttk.Frame(notebook, padding=22)
        notebook.add(general, text="Generali")
        notebook.add(controller, text="Controller")
        notebook.add(pdf_print, text="PDF / stampa")
        notebook.add(retention, text="Retention")
        notebook.add(backup, text="Backup")

        general.columnconfigure(1, weight=1)
        ttk.Label(
            general,
            text="Preferenze generali",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            general,
            text="Imposta l'identità visibile della struttura e il tema dell'app.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 16))
        ttk.Label(general, text="Profilo struttura").grid(
            row=2, column=0, sticky="w", padx=(0, 18), pady=7
        )
        ttk.Combobox(
            general,
            textvariable=self.settings_structure_type_var,
            state="readonly",
            values=("Sede", "Evento", "Personalizzata"),
        ).grid(row=2, column=1, sticky="ew", pady=7)
        ttk.Label(general, text="Nome struttura").grid(
            row=3, column=0, sticky="w", padx=(0, 18), pady=7
        )
        ttk.Entry(
            general,
            textvariable=self.settings_structure_name_var,
        ).grid(row=3, column=1, sticky="ew", pady=7)
        ttk.Label(general, text="Titolo Wi-Fi").grid(
            row=4, column=0, sticky="w", padx=(0, 18), pady=7
        )
        ttk.Entry(
            general,
            textvariable=self.settings_wifi_title_var,
        ).grid(row=4, column=1, sticky="ew", pady=7)
        ttk.Label(general, text="Tema").grid(
            row=5, column=0, sticky="w", padx=(0, 18), pady=7
        )
        ttk.Combobox(
            general,
            textvariable=self.settings_theme_var,
            state="readonly",
            values=("system", "light", "dark"),
            width=18,
        ).grid(row=5, column=1, sticky="w", pady=7)
        ttk.Label(
            general,
            text="“system” segue automaticamente il tema chiaro/scuro di Windows.",
            style="Muted.TLabel",
        ).grid(row=6, column=1, sticky="w", pady=(2, 0))

        controller.columnconfigure(1, weight=1)
        ttk.Label(
            controller,
            text="Controller UniFi",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            controller,
            text=(
                "Questa sezione contiene i dettagli tecnici della connessione. "
                "Non sono necessari durante l'uso quotidiano."
            ),
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 16))
        ttk.Label(controller, text="Indirizzo controller").grid(
            row=2, column=0, sticky="w", padx=(0, 14), pady=7
        )
        self.api_root_entry = ttk.Entry(
            controller,
            textvariable=self.api_root_var,
        )
        self.api_root_entry.grid(row=2, column=1, sticky="ew", pady=7)
        self.api_root_entry.bind("<Return>", lambda _event: self.connect())
        ttk.Label(controller, text="API key").grid(
            row=3, column=0, sticky="w", padx=(0, 14), pady=7
        )
        self.api_key_entry = ttk.Entry(
            controller,
            textvariable=self.api_key_var,
            show="•",
        )
        self.api_key_entry.grid(row=3, column=1, sticky="ew", pady=7)
        self.api_key_entry.bind("<Return>", lambda _event: self.connect())
        self.connect_button = ttk.Button(
            controller,
            text="Verifica / connetti",
            command=self.connect,
            style="Accent.TButton",
        )
        self.connect_button.grid(row=2, column=2, rowspan=2, padx=(14, 0))
        ttk.Separator(controller).grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=16
        )
        ttk.Label(
            controller,
            text="Stato",
            style="Muted.TLabel",
        ).grid(row=5, column=0, sticky="nw")
        ttk.Label(
            controller,
            textvariable=self.controller_health_var,
            style="Status.TLabel",
        ).grid(row=5, column=1, columnspan=2, sticky="w")
        ttk.Label(
            controller,
            textvariable=self.controller_health_detail_var,
            style="Muted.TLabel",
            wraplength=650,
        ).grid(row=6, column=1, columnspan=2, sticky="w", pady=(3, 12))
        ttk.Label(
            controller,
            text=(
                "La API key resta solo in memoria durante la sessione e non "
                "viene salvata."
            ),
            style="Muted.TLabel",
        ).grid(row=7, column=0, columnspan=3, sticky="w")

        pdf_print.columnconfigure(1, weight=1)
        ttk.Label(
            pdf_print,
            text="PDF e stampa",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            pdf_print,
            text="Personalizza il documento senza modificare il layout operativo dei voucher.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 16))
        ttk.Label(pdf_print, text="Preset grafico").grid(
            row=2, column=0, sticky="w", padx=(0, 18), pady=7
        )
        ttk.Combobox(
            pdf_print,
            textvariable=self.settings_preset_var,
            state="readonly",
            values=("Classico", "Minimal", "Contrasto", "Personalizzato"),
        ).grid(row=2, column=1, columnspan=2, sticky="ew", pady=7)
        ttk.Label(pdf_print, text="Logo").grid(
            row=3, column=0, sticky="w", padx=(0, 18), pady=7
        )
        ttk.Entry(
            pdf_print,
            textvariable=self.settings_logo_var,
            state="readonly",
        ).grid(row=3, column=1, sticky="ew", pady=7)
        ttk.Button(
            pdf_print,
            text="Scegli…",
            command=self._choose_settings_logo,
        ).grid(row=3, column=2, padx=(8, 0))
        ttk.Button(
            pdf_print,
            text="Usa predefinito",
            command=lambda: self.settings_logo_var.set(""),
        ).grid(row=4, column=1, sticky="w")
        ttk.Label(pdf_print, text="Conservazione PDF").grid(
            row=5, column=0, sticky="w", padx=(0, 18), pady=(18, 7)
        )
        pdf_days = ttk.Frame(pdf_print)
        pdf_days.grid(row=5, column=1, columnspan=2, sticky="w", pady=(18, 7))
        ttk.Spinbox(
            pdf_days,
            from_=0,
            to=3650,
            increment=30,
            textvariable=self.settings_pdf_retention_var,
            width=8,
        ).pack(side="left")
        ttk.Label(pdf_days, text="giorni   (0 = conserva sempre)").pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            pdf_print,
            text=f"Layout A4: {VOUCHERS_PER_PAGE} voucher per pagina.",
            style="Muted.TLabel",
        ).grid(row=6, column=1, sticky="w", pady=(2, 0))

        ttk.Label(
            retention,
            text="Conservazione dello storico",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            retention,
            textvariable=self.settings_retention_summary_var,
            style="Body.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(8, 12))
        ttk.Label(
            retention,
            text=(
                "Voucher utilizzati o stampati restano protetti. Gli altri "
                "possono diventare candidati solo dopo il periodo configurato "
                "e vengono sempre mostrati prima di qualsiasi minimizzazione."
            ),
            style="Muted.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(0, 14))
        ttk.Button(
            retention,
            text="Rivedi conservazione…",
            command=lambda: self.open_retention_review(parent=self),
            style="Accent.TButton",
        ).pack(anchor="w")

        ttk.Label(
            backup,
            text="Backup e ripristino",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            backup,
            textvariable=self.settings_backup_summary_var,
            style="Body.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(8, 12))
        ttk.Checkbutton(
            backup,
            text="Crea un backup protetto prima della chiusura (consigliato)",
            variable=self.settings_backup_on_close_var,
        ).pack(anchor="w", pady=(0, 12))
        backup_actions = ttk.Frame(backup)
        backup_actions.pack(anchor="w")
        ttk.Button(
            backup_actions,
            text="Crea backup…",
            command=lambda: self.create_backup(parent=self),
            style="Accent.TButton",
        ).pack(side="left")
        ttk.Button(
            backup_actions,
            text="Ripristina backup…",
            command=lambda: self.restore_backup(parent=self),
        ).pack(side="left", padx=(8, 0))

        ttk.Separator(backup).pack(fill="x", pady=18)
        ttk.Label(
            backup,
            text="Recupero e manutenzione",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            backup,
            text=(
                "Strumenti da usare solo quando serve recuperare cronologia, "
                "una stampa pendente o dati di una versione precedente."
            ),
            style="Muted.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(3, 10))
        maintenance = ttk.Frame(backup)
        maintenance.pack(anchor="w")
        ttk.Button(
            maintenance,
            text="Verifica cronologia…",
            command=lambda: HistoryRecoveryDialog(self),
        ).pack(side="left")
        ttk.Button(
            maintenance,
            text="Recupera stampa pendente…",
            command=lambda: self.recover_pending_print_audit(parent=self),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            maintenance,
            text="Importa cronologia…",
            command=lambda: self.import_history_exchange(parent=self),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            maintenance,
            text="Esporta cronologia…",
            command=lambda: self.export_history_exchange(parent=self),
        ).pack(side="left", padx=(8, 0))
        if getattr(self.paths, "shared_mode", False):
            ttk.Button(
                backup,
                text="Migra dati del profilo Windows…",
                command=lambda: self.migrate_per_user_data_to_shared(parent=self),
            ).pack(anchor="w", pady=(12, 0))
        ttk.Button(
            backup,
            text="Analizza storico 4.x…",
            command=lambda: self.migrate_legacy_history(parent=self),
        ).pack(anchor="w", pady=(8, 0))

        footer = ttk.Frame(frame)
        footer.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(
            footer,
            textvariable=self.settings_save_status_var,
            style="Muted.TLabel",
        ).pack(side="left")
        self.settings_save_button = ttk.Button(
            footer,
            text="Salva modifiche",
            command=self._save_workspace_settings,
            style="Accent.TButton",
        )
        self.settings_save_button.pack(side="right")

        self._load_settings_workspace_values()
        self._refresh_backup_summary()
        self._refresh_retention_summary()

    def _load_settings_workspace_values(self) -> None:
        if not hasattr(self, "settings_structure_name_var"):
            return
        settings = self.settings_store.load()
        self.settings = settings
        self.settings_structure_type_var.set(
            str(settings.get("structure_type", DEFAULT_STRUCTURE_TYPE))
        )
        self.settings_structure_name_var.set(
            str(settings.get("structure_name", "") or "")
        )
        self.settings_wifi_title_var.set(
            str(settings.get("wifi_title", DEFAULT_WIFI_TITLE) or DEFAULT_WIFI_TITLE)
        )
        self.settings_theme_var.set(
            str(settings.get("ui_theme", "system") or "system")
        )
        self.settings_preset_var.set(
            str(settings.get("preset", "Classico") or "Classico")
        )
        self.settings_logo_var.set(
            str(settings.get("logo_path", "") or "")
        )
        self.settings_pdf_retention_var.set(
            str(settings.get("print_retention_days", DEFAULT_PRINT_RETENTION_DAYS))
        )
        self.settings_backup_on_close_var.set(
            bool(settings.get("backup_on_close", True))
        )
        self.settings_save_status_var.set("")

    def _choose_settings_logo(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self,
            title="Scegli logo",
            filetypes=[("Immagini", "*.png *.jpg *.jpeg")],
        )
        if not selected:
            return
        try:
            validate_logo_image(Path(selected))
        except LogoValidationError as exc:
            messagebox.showerror("Logo non valido", str(exc), parent=self)
            return
        self.settings_logo_var.set(selected)

    def _save_workspace_settings(self) -> None:
        try:
            pdf_retention_days = int(self.settings_pdf_retention_var.get())
            if not 0 <= pdf_retention_days <= 3650:
                raise ValueError
        except (TypeError, ValueError, tk.TclError):
            messagebox.showerror(
                "Impostazioni",
                "La conservazione PDF deve essere compresa tra 0 e 3650 giorni.",
                parent=self,
            )
            return

        structure_name = self.settings_structure_name_var.get().strip()
        wifi_title = self.settings_wifi_title_var.get().strip()
        if not structure_name or not wifi_title:
            messagebox.showerror(
                "Impostazioni",
                "Inserire nome struttura e titolo Wi-Fi.",
                parent=self,
            )
            return

        logo_value = self.settings_logo_var.get().strip()
        try:
            if logo_value:
                validate_logo_image(Path(logo_value))
                logo_value = self.paths.persist_configured_logo(logo_value)
                if not logo_value:
                    raise LogoValidationError(
                        "Il logo non può essere salvato nella libreria locale"
                    )
        except (LogoValidationError, OSError) as exc:
            messagebox.showerror("Logo non valido", str(exc), parent=self)
            return

        previous_theme = str(self.settings.get("ui_theme", "system"))
        previous_pdf_retention = int(
            self.settings.get(
                "print_retention_days",
                DEFAULT_PRINT_RETENTION_DAYS,
            )
        )
        self.settings = self.settings_store.update(
            structure_type=self.settings_structure_type_var.get().strip()
            or DEFAULT_STRUCTURE_TYPE,
            structure_name=structure_name,
            wifi_title=wifi_title,
            preset=self.settings_preset_var.get().strip() or "Classico",
            logo_path=logo_value,
            ui_theme=self.settings_theme_var.get().strip() or "system",
            print_retention_days=pdf_retention_days,
            backup_on_close=bool(self.settings_backup_on_close_var.get()),
        )
        self.settings_logo_var.set(logo_value)
        self.installation_display_var.set(
            structure_name or self.installation_display_var.get()
        )
        if previous_theme != self.settings.get("ui_theme"):
            self.apply_theme()
            self._configure_style()
        if previous_pdf_retention != pdf_retention_days:
            self._cleanup_print_archive()
        self.settings_save_status_var.set("Modifiche salvate.")
        self.after(3500, lambda: self.settings_save_status_var.set(""))

    def _refresh_backup_summary(self) -> None:
        if not hasattr(self, "settings_backup_summary_var"):
            return
        row = self.database.connection.execute(
            """SELECT completed_at, filename
               FROM backup_history
               WHERE status='SUCCESS'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            self.settings_backup_summary_var.set(
                "Nessun backup completato è ancora registrato su questa postazione."
            )
            return
        when = audit_time_label(str(row["completed_at"] or ""))
        filename = str(row["filename"] or "").strip() or "backup protetto"
        self.settings_backup_summary_var.set(
            f"Ultimo backup: {when}  •  {filename}"
        )

    def _refresh_retention_summary(self) -> None:
        if not hasattr(self, "settings_retention_summary_var"):
            return
        policy = self.database.retention_policy()
        if policy is None:
            self.settings_retention_summary_var.set(
                "Conservazione voucher non ancora configurata."
            )
            return
        self.settings_retention_summary_var.set(
            "I voucher mai usati e mai stampati diventano candidati dopo "
            f"{int(policy['unused_unprinted_days'])} giorni."
        )

    def _show_workspace(self, key: str) -> None:
        titles = {
            "home": (
                "Home",
                "Panoramica generale e accesso rapido alle principali funzioni",
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
                "Generali, controller, PDF, retention e backup",
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
        elif key == "settings":
            self._load_settings_workspace_values()
            self._refresh_backup_summary()
            self._refresh_retention_summary()
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
        friendly_sync = audit_time_label(last_sync) if last_sync else "Mai"
        self.controller_health_var.set(status.title)
        self.controller_health_detail_var.set(status.detail)
        self.home_ready_var.set(status.title)
        self.home_controller_name_var.set(name or "Nessun controller configurato")
        self.home_last_sync_var.set(friendly_sync)
        self.home_sync_detail_var.set(status.detail)
        self.sidebar_sync_var.set(
            "Ultimo aggiornamento\n"
            + (friendly_sync if friendly_sync != "—" else "Mai")
        )
        self._controller_workspace_status = status

        status_style = {
            "connected": "Success.Status.TLabel",
            "syncing": "Busy.Status.TLabel",
            "error": "Error.Status.TLabel",
            "local": "Warning.Status.TLabel",
            "unconfigured": "Warning.Status.TLabel",
        }.get(status.key, "Status.TLabel")
        sidebar_style = {
            "connected": "SidebarSuccess.TLabel",
            "syncing": "SidebarBusy.TLabel",
            "error": "SidebarError.TLabel",
            "local": "SidebarWarning.TLabel",
            "unconfigured": "SidebarWarning.TLabel",
        }.get(status.key, "SidebarStatus.TLabel")
        for widget_name in ("home_status_title_label",):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.configure(style=status_style)
        sidebar_label = getattr(self, "sidebar_status_label", None)
        if sidebar_label is not None:
            sidebar_label.configure(style=sidebar_style)

        if status.key == "connected":
            self.home_sync_action_var.set("Sincronizza")
        elif status.key == "unconfigured":
            self.home_sync_action_var.set("Configura controller")
        else:
            self.home_sync_action_var.set("Riconnetti")

    def _home_sync_or_connect(self) -> None:
        if self.client is not None:
            self.refresh()
            return
        self._show_workspace("settings")
        try:
            self.api_key_entry.focus_set()
        except tk.TclError:
            pass

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
        """Build a theme-aware Windows hierarchy without turning it into a web UI."""

        style = ttk.Style(self)
        preference = str(self.settings.get("ui_theme", "system") or "system")
        dark = darkdetect.isDark() if preference == "system" else preference == "dark"
        sidebar_bg = "#20242a" if dark else "#eef2f7"
        sidebar_fg = "#f5f7fa" if dark else "#172033"
        sidebar_muted = "#c3cad4" if dark else "#5f6b7a"

        style.configure("Sidebar.TFrame", background=sidebar_bg)
        style.configure(
            "SidebarBrand.TLabel",
            background=sidebar_bg,
            foreground=sidebar_fg,
            font=("Segoe UI Variable Display", 16, "bold"),
        )
        style.configure(
            "SidebarMuted.TLabel",
            background=sidebar_bg,
            foreground=sidebar_muted,
            font=("Segoe UI Variable Text", 9),
        )
        style.configure(
            "SidebarStatus.TLabel",
            background=sidebar_bg,
            foreground=sidebar_fg,
            font=("Segoe UI Variable Text", 10, "bold"),
        )
        for name, foreground in (
            ("SidebarSuccess.TLabel", "#22a447"),
            ("SidebarBusy.TLabel", "#2585d8"),
            ("SidebarWarning.TLabel", "#c47a00"),
            ("SidebarError.TLabel", "#d13438"),
        ):
            style.configure(
                name,
                background=sidebar_bg,
                foreground=foreground,
                font=("Segoe UI Variable Text", 10, "bold"),
            )

        style.configure(
            "PageTitle.TLabel",
            font=("Segoe UI Variable Display", 24, "bold"),
        )
        style.configure(
            "HeroTitle.TLabel",
            font=("Segoe UI Variable Display", 18, "bold"),
        )
        style.configure(
            "HeaderMeta.TLabel",
            font=("Segoe UI Variable Text", 10, "bold"),
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
            font=("Segoe UI Variable Text", 11, "bold"),
        )
        for name, foreground in (
            ("Success.Status.TLabel", "#107c10"),
            ("Busy.Status.TLabel", "#0067c0"),
            ("Warning.Status.TLabel", "#9a6500"),
            ("Error.Status.TLabel", "#c42b1c"),
        ):
            style.configure(
                name,
                foreground=foreground,
                font=("Segoe UI Variable Text", 11, "bold"),
            )
        style.configure(
            "Metric.TLabel",
            font=("Segoe UI Variable Display", 26, "bold"),
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
            "Card.TLabelframe",
            padding=(2, 2),
        )
        style.configure(
            "Card.TLabelframe.Label",
            font=("Segoe UI Variable Text", 11, "bold"),
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
        self.minsize(1220, 740)

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
                    voucher.code_formatted,
                    voucher.recipient or "—",
                    state,
                    time_label(voucher.end_time),
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
            for (
                _order,
                code,
                recipient,
                state,
                expires,
                created,
            ) in sorted(recent_rows)[:7]:
                self.home_recent_tree.insert(
                    "",
                    "end",
                    values=(code, recipient, state, expires, created),
                )
        self._refresh_home_activity()
        self._refresh_report_summary()
        self._refresh_controller_workspace_status()

    def _refresh_home_activity(self) -> None:
        if not hasattr(self, "home_activity_tree"):
            return
        try:
            items = load_recent_workspace_activity(
                self.database,
                controller_id=self.active_controller_id,
                limit=7,
            )
        except Exception as exc:
            self.logger.warning(
                "workspace_activity_load_failed type=%s",
                type(exc).__name__,
            )
            return
        for iid in self.home_activity_tree.get_children():
            self.home_activity_tree.delete(iid)
        for item in items:
            self.home_activity_tree.insert(
                "",
                "end",
                values=(
                    audit_time_label(item.occurred_at),
                    item.title,
                    item.detail,
                ),
            )

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
