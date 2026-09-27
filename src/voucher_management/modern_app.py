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
from .onboarding import LEGACY_MIGRATION_DECLINED_KEY, OnboardingState
from .onboarding_ui import FirstRunWizard, schedule_first_run_onboarding, startup_onboarding_state
from .pdf_render import VOUCHERS_PER_PAGE
from .print_archive import DEFAULT_PRINT_RETENTION_DAYS
from .report_ui import ReportDialog
from .retention_ui import RetentionMixin
from .sync_store import load_local_vouchers
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
    """Operator settings split by task instead of implementation detail."""

    TAB_NAMES = (
        "Generali",
        "Controller",
        "PDF / stampa",
        "Retention",
        "Backup",
    )

    def __init__(
        self,
        app: "ModernVoucherApp",
        *,
        initial_tab: str = "Generali",
    ):
        super().__init__(app)
        self.app = app
        self.title("Impostazioni")
        self.transient(app)
        self.grab_set()

        s = app.settings
        self.structure_type = tk.StringVar(
            value=s.get("structure_type", DEFAULT_STRUCTURE_TYPE)
        )
        self.structure_name = tk.StringVar(
            value=s.get("structure_name", DEFAULT_STRUCTURE_NAME)
        )
        self.wifi_title = tk.StringVar(
            value=s.get("wifi_title", DEFAULT_WIFI_TITLE)
        )
        self.preset = tk.StringVar(value=s.get("preset", "Classico"))
        self.logo = tk.StringVar(value=s.get("logo_path", ""))
        self.theme = tk.StringVar(value=s.get("ui_theme", "system"))
        try:
            pdf_retention = int(
                s.get("print_retention_days", DEFAULT_PRINT_RETENTION_DAYS)
            )
        except (TypeError, ValueError):
            pdf_retention = DEFAULT_PRINT_RETENTION_DAYS
        self.print_retention_days = tk.StringVar(value=str(pdf_retention))
        self.backup_on_close = tk.BooleanVar(
            value=bool(s.get("backup_on_close", True))
        )

        if not hasattr(app, "controller_profile_name_var"):
            current_name = (
                app.database.controller_name(app.active_controller_id)
                if app.active_controller_id is not None
                else None
            )
            app.controller_profile_name_var = tk.StringVar(
                value=current_name or "Controller UniFi"
            )
        self.controller_choice_var = tk.StringVar()
        self._controller_profiles_by_label = {}

        shell = ttk.Frame(self, padding=22)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text="Impostazioni",
            style="PageTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text="Configura solo ciò che serve alla postazione.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 16))

        self.tabs = ttk.Notebook(shell)
        self.tabs.pack(fill="both", expand=True)
        frames = {}
        for name in self.TAB_NAMES:
            frame = ttk.Frame(self.tabs, padding=20)
            self.tabs.add(frame, text=name)
            frames[name] = frame

        self._build_general_tab(frames["Generali"])
        self._build_controller_tab(frames["Controller"])
        self._build_pdf_tab(frames["PDF / stampa"])
        self._build_retention_tab(frames["Retention"])
        self._build_backup_tab(frames["Backup"])

        if initial_tab in self.TAB_NAMES:
            self.tabs.select(self.TAB_NAMES.index(initial_tab))

        footer = ttk.Frame(shell)
        footer.pack(fill="x", pady=(16, 0))
        ttk.Button(
            footer,
            text="Annulla",
            command=self.destroy,
            width=12,
        ).pack(side="right")
        ttk.Button(
            footer,
            text="Salva",
            command=self.save,
            style="Accent.TButton",
            width=12,
        ).pack(side="right", padx=(0, 8))
        fit_dialog(self, app, min_width=820, min_height=620)

    def _build_general_tab(self, frame: ttk.Frame) -> None:
        ttk.Label(
            frame,
            text="Postazione",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            frame,
            text=(
                "Questi valori sono mostrati nei voucher e identificano "
                "la postazione per l'operatore."
            ),
            style="Muted.TLabel",
            wraplength=620,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 14))

        rows = (
            ("Profilo struttura", self.structure_type),
            ("Nome struttura", self.structure_name),
            ("Titolo Wi-Fi", self.wifi_title),
        )
        profile_values = ("Sede", "Evento", "Personalizzata")
        current_profile = self.structure_type.get().strip()
        if current_profile and current_profile not in profile_values:
            profile_values = (current_profile, *profile_values)

        for offset, (label, variable) in enumerate(rows, start=2):
            ttk.Label(frame, text=label).grid(
                row=offset,
                column=0,
                sticky="w",
                pady=7,
                padx=(0, 18),
            )
            if offset == 2:
                widget = ttk.Combobox(
                    frame,
                    textvariable=variable,
                    state="readonly",
                    values=profile_values,
                )
            else:
                widget = ttk.Entry(frame, textvariable=variable)
            widget.grid(row=offset, column=1, sticky="ew", pady=7)

        ttk.Separator(frame).grid(
            row=5,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=20,
        )
        ttk.Label(
            frame,
            text="Aspetto",
            style="SectionTitle.TLabel",
        ).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(
            frame,
            text="Tema applicazione",
        ).grid(row=7, column=0, sticky="w", pady=7)
        ttk.Combobox(
            frame,
            textvariable=self.theme,
            state="readonly",
            values=("system", "light", "dark"),
            width=18,
        ).grid(row=7, column=1, sticky="w", pady=7)
        ttk.Label(
            frame,
            text="Sistema segue automaticamente il tema chiaro/scuro di Windows.",
            style="Muted.TLabel",
            wraplength=620,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(3, 0))
        frame.columnconfigure(1, weight=1)

    def _build_controller_tab(self, frame: ttk.Frame) -> None:
        ttk.Label(
            frame,
            text="Controller UniFi",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            frame,
            text=(
                "Seleziona un profilo salvato oppure preparane uno nuovo. "
                "La API key serve solo per la connessione corrente e non "
                "viene salvata."
            ),
            style="Muted.TLabel",
            wraplength=650,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 14))

        ttk.Label(frame, text="Profili salvati").grid(
            row=2, column=0, sticky="w", pady=7, padx=(0, 18)
        )
        profile_row = ttk.Frame(frame)
        profile_row.grid(row=2, column=1, columnspan=2, sticky="ew", pady=7)
        profile_row.columnconfigure(0, weight=1)
        self.controller_combo = ttk.Combobox(
            profile_row,
            textvariable=self.controller_choice_var,
            state="readonly",
        )
        self.controller_combo.grid(row=0, column=0, sticky="ew")
        self.controller_combo.bind(
            "<<ComboboxSelected>>",
            self._select_controller_profile,
        )
        ttk.Button(
            profile_row,
            text="Nuovo",
            command=self._new_controller_profile,
        ).grid(row=0, column=1, padx=(8, 0))

        ttk.Label(frame, text="Nome profilo").grid(
            row=3, column=0, sticky="w", pady=7, padx=(0, 18)
        )
        name_row = ttk.Frame(frame)
        name_row.grid(row=3, column=1, columnspan=2, sticky="ew", pady=7)
        name_row.columnconfigure(0, weight=1)
        ttk.Entry(
            name_row,
            textvariable=self.app.controller_profile_name_var,
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            name_row,
            text="Rinomina",
            command=self._rename_controller_profile,
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            name_row,
            text="Rimuovi",
            command=self._remove_controller_profile,
        ).grid(row=0, column=2, padx=(8, 0))

        ttk.Label(frame, text="Indirizzo controller").grid(
            row=4, column=0, sticky="w", pady=7, padx=(0, 18)
        )
        ttk.Entry(
            frame,
            textvariable=self.app.api_root_var,
        ).grid(row=4, column=1, columnspan=2, sticky="ew", pady=7)

        ttk.Label(frame, text="API key").grid(
            row=5, column=0, sticky="w", pady=7, padx=(0, 18)
        )
        key_row = ttk.Frame(frame)
        key_row.grid(row=5, column=1, columnspan=2, sticky="ew", pady=7)
        key_row.columnconfigure(0, weight=1)
        ttk.Entry(
            key_row,
            textvariable=self.app.api_key_var,
            show="•",
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            key_row,
            text="Verifica / Connetti",
            command=self.app.connect,
            style="Accent.TButton",
        ).grid(row=0, column=1, padx=(8, 0))

        ttk.Label(
            frame,
            textvariable=self.app.connection_var,
            style="ConnectionStatus.TLabel",
            wraplength=650,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Label(
            frame,
            text=(
                "Rimuovere un profilo non elimina voucher, stampe o storico: "
                "nasconde soltanto quella configurazione. Se viene aggiunto "
                "di nuovo lo stesso controller, la sua identità storica viene "
                "riutilizzata."
            ),
            style="Muted.TLabel",
            wraplength=650,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
        frame.columnconfigure(1, weight=1)
        self._refresh_controller_profiles()

    def _controller_label(self, profile: dict) -> str:
        name = str(profile.get("name") or "Controller").strip()
        root = str(profile.get("api_root") or "").strip()
        return f"{name} — {root}"

    def _refresh_controller_profiles(
        self,
        *,
        select_id: int | None = None,
    ) -> None:
        profiles = self.app.database.controller_profiles()
        mapping = {
            self._controller_label(profile): profile
            for profile in profiles
        }
        self._controller_profiles_by_label = mapping
        self.controller_combo["values"] = tuple(mapping)

        wanted_id = (
            select_id
            if select_id is not None
            else self.app.active_controller_id
        )
        selected_label = ""
        for label, profile in mapping.items():
            if wanted_id is not None and int(profile["id"]) == int(wanted_id):
                selected_label = label
                break
        self.controller_choice_var.set(selected_label)

    def _selected_controller_profile(self) -> dict | None:
        return self._controller_profiles_by_label.get(
            self.controller_choice_var.get()
        )

    def _select_controller_profile(self, _event=None) -> None:
        profile = self._selected_controller_profile()
        if profile is None:
            return
        self.app.select_controller_profile(profile)

    def _new_controller_profile(self) -> None:
        self.controller_choice_var.set("")
        self.app.controller_profile_name_var.set("Controller UniFi")
        self.app.api_root_var.set("")
        self.app.api_key_var.set("")
        self.app.connection_var.set(
            "Nuovo profilo: inserire indirizzo e API key per verificarlo"
        )

    def _rename_controller_profile(self) -> None:
        profile = self._selected_controller_profile()
        if profile is None:
            messagebox.showinfo(
                "Controller",
                "Selezionare prima un profilo salvato.",
                parent=self,
            )
            return
        name = self.app.controller_profile_name_var.get().strip()
        if not name:
            messagebox.showerror(
                "Controller",
                "Inserire un nome per il profilo.",
                parent=self,
            )
            return
        try:
            self.app.database.rename_controller(int(profile["id"]), name)
        except Exception as exc:
            self.app.logger.warning(
                "controller_profile_rename_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Controller",
                "Impossibile rinominare il profilo.",
                parent=self,
            )
            return
        self._refresh_controller_profiles(select_id=int(profile["id"]))

    def _remove_controller_profile(self) -> None:
        profile = self._selected_controller_profile()
        if profile is None:
            messagebox.showinfo(
                "Controller",
                "Selezionare prima un profilo salvato.",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "Rimuovi controller",
            "Rimuovere questo profilo dalla configurazione?\n\n"
            "Voucher, stampe e storico restano conservati.",
            parent=self,
        ):
            return
        controller_id = int(profile["id"])
        try:
            self.app.database.deactivate_controller(controller_id)
        except Exception as exc:
            self.app.logger.warning(
                "controller_profile_remove_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Controller",
                "Impossibile rimuovere il profilo.",
                parent=self,
            )
            return

        if self.app.active_controller_id == controller_id:
            self.app.clear_active_controller_profile()
        self._refresh_controller_profiles()

    def _build_pdf_tab(self, frame: ttk.Frame) -> None:
        ttk.Label(
            frame,
            text="Documento voucher",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            frame,
            text=(
                "Il formato operativo dei voucher resta invariato. "
                "Qui puoi scegliere aspetto e logo."
            ),
            style="Muted.TLabel",
            wraplength=620,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 14))

        ttk.Label(frame, text="Preset grafico").grid(
            row=2, column=0, sticky="w", pady=8, padx=(0, 18)
        )
        ttk.Combobox(
            frame,
            textvariable=self.preset,
            state="readonly",
            values=("Classico", "Minimal", "Contrasto", "Personalizzato"),
        ).grid(row=2, column=1, columnspan=2, sticky="ew", pady=8)

        ttk.Label(frame, text="Logo").grid(
            row=3, column=0, sticky="w", pady=8, padx=(0, 18)
        )
        self.logo_combo = ttk.Combobox(frame, state="readonly")
        self.logo_combo.grid(row=3, column=1, sticky="ew", pady=8)
        self.logo_combo.bind("<<ComboboxSelected>>", self._select_logo)
        ttk.Button(
            frame,
            text="Aggiungi…",
            command=self.add_logo,
        ).grid(row=3, column=2, padx=(8, 0))
        ttk.Button(
            frame,
            text="Usa predefinito",
            command=self.use_default,
        ).grid(row=4, column=1, sticky="w")

        ttk.Separator(frame).grid(
            row=5,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=20,
        )
        ttk.Label(
            frame,
            text="Archivio PDF",
            style="SectionTitle.TLabel",
        ).grid(row=6, column=0, columnspan=3, sticky="w")
        ttk.Label(
            frame,
            text=(
                "0 giorni = conserva sempre. La pulizia riguarda soltanto "
                "documenti riconosciuti come creati da Voucher Management."
            ),
            style="Muted.TLabel",
            wraplength=620,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(3, 10))
        ttk.Label(frame, text="Conservazione PDF").grid(
            row=8, column=0, sticky="w", pady=7, padx=(0, 18)
        )
        ttk.Spinbox(
            frame,
            from_=0,
            to=3650,
            increment=30,
            textvariable=self.print_retention_days,
            width=8,
        ).grid(row=8, column=1, sticky="w", pady=7)
        ttk.Label(
            frame,
            text=f"Layout A4: {VOUCHERS_PER_PAGE} voucher per pagina.",
            style="Muted.TLabel",
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(8, 0))
        frame.columnconfigure(1, weight=1)
        self._refresh_logos()

    def _build_retention_tab(self, frame: ttk.Frame) -> None:
        policy = self.app.database.retention_policy()
        days = (
            int(policy["unused_unprinted_days"])
            if policy is not None
            else 180
        )
        ttk.Label(
            frame,
            text="Conservazione voucher",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                f"Impostazione attuale: {days} giorni. "
                "I voucher usati o fisicamente stampati restano sempre "
                "protetti. I voucher inutilizzati vengono soltanto proposti "
                "per la revisione quando non sono più presenti sul controller."
            ),
            style="Muted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(4, 16))
        ttk.Button(
            frame,
            text="Rivedi conservazione…",
            command=lambda: self.app.open_retention_review(parent=self),
            style="Accent.TButton",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "Nessuna minimizzazione avviene automaticamente: "
                "l'operatore deve sempre scegliere le righe da applicare."
            ),
            style="Muted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(12, 0))

    def _build_backup_tab(self, frame: ttk.Frame) -> None:
        ttk.Label(
            frame,
            text="Backup",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "Crea una copia protetta dei dati della postazione o ripristina "
                "un backup precedente. La password viene richiesta solo quando "
                "serve e non viene memorizzata."
            ),
            style="Muted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(4, 12))
        ttk.Checkbutton(
            frame,
            text="Crea un backup prima della chiusura (consigliato)",
            variable=self.backup_on_close,
        ).pack(anchor="w", pady=(0, 12))
        actions = ttk.Frame(frame)
        actions.pack(anchor="w")
        ttk.Button(
            actions,
            text="Crea backup…",
            command=lambda: self.app.create_backup(parent=self),
            style="Accent.TButton",
        ).pack(side="left")
        ttk.Button(
            actions,
            text="Ripristina backup…",
            command=lambda: self.app.restore_backup(parent=self),
        ).pack(side="left", padx=(8, 0))

        ttk.Separator(frame).pack(fill="x", pady=22)
        ttk.Label(
            frame,
            text="Strumenti dati avanzati",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "Usare questi strumenti solo per recupero, trasferimento "
                "o aggiornamento di installazioni precedenti."
            ),
            style="Muted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(4, 10))
        advanced = ttk.Frame(frame)
        advanced.pack(anchor="w")
        ttk.Button(
            advanced,
            text="Recupera cronologia…",
            command=lambda: HistoryRecoveryDialog(self.app),
        ).pack(side="left")
        ttk.Button(
            advanced,
            text="Recupera stampa pendente…",
            command=lambda: self.app.recover_pending_print_audit(parent=self),
        ).pack(side="left", padx=(8, 0))
        if getattr(self.app.paths, "shared_mode", False):
            ttk.Button(
                advanced,
                text="Migra dati precedenti…",
                command=lambda: self.app.migrate_per_user_data_to_shared(
                    parent=self,
                ),
            ).pack(side="left", padx=(8, 0))

    def _logo_files(self) -> list[Path]:
        return sorted(
            (
                path
                for path in self.app.paths.logos.iterdir()
                if path.suffix.lower() in {".png", ".jpg", ".jpeg"}
            ),
            key=lambda path: path.name.lower(),
        )

    def _refresh_logos(self, select: Path | None = None) -> None:
        files = self._logo_files()
        self.logo_combo["values"] = [path.name for path in files]
        current = select or (
            Path(self.logo.get()) if self.logo.get() else None
        )
        if (
            current
            and current.exists()
            and current.parent == self.app.paths.logos
        ):
            self.logo_combo.set(current.name)
        elif not self.logo.get():
            self.logo_combo.set("Predefinito")

    def _select_logo(self, _event=None) -> None:
        name = self.logo_combo.get().strip()
        if name and name != "Predefinito":
            self.logo.set(str(self.app.paths.logos / name))

    def add_logo(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self,
            title="Aggiungi un logo",
            filetypes=[("Immagini", "*.png *.jpg *.jpeg")],
        )
        if not selected:
            return
        source = Path(selected)
        try:
            validate_logo_image(source)
        except LogoValidationError as exc:
            messagebox.showerror(
                "Logo non valido",
                str(exc),
                parent=self,
            )
            return

        target = self.app.paths.logos / source.name
        counter = 2
        while target.exists() and target.resolve() != source.resolve():
            target = (
                self.app.paths.logos
                / f"{source.stem}_{counter}{source.suffix.lower()}"
            )
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
            pdf_retention = int(self.print_retention_days.get())
            if not 0 <= pdf_retention <= 3650:
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
            self.app.settings.get(
                "print_retention_days",
                DEFAULT_PRINT_RETENTION_DAYS,
            )
        )
        self.app.settings = self.app.settings_store.update(
            structure_type=self.structure_type.get(),
            structure_name=self.structure_name.get().strip(),
            wifi_title=self.wifi_title.get().strip(),
            preset=self.preset.get(),
            logo_path=self.logo.get().strip(),
            ui_theme=self.theme.get(),
            print_retention_days=pdf_retention,
            backup_on_close=bool(self.backup_on_close.get()),
        )
        if old_theme != self.theme.get():
            self.app.apply_theme()
        if old_retention != pdf_retention:
            self.app._cleanup_print_archive()
        self.app._refresh_workspace_summary()
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
            text="Migra dati…",
            command=lambda: app.migrate_per_user_data_to_shared(parent=self),
            style="Accent.TButton",
        ).pack(side="right", padx=(0, 8))
        fit_dialog(self, app, min_width=760, min_height=290)

    def _start_fresh_installation(self) -> None:
        if not messagebox.askyesno(
            "Nuova installazione",
            "I dati precedenti nel profilo Windows verranno lasciati intatti "
            "ma non saranno importati in questa installazione condivisa.\n\n"
            "Continuare con una nuova configurazione?",
            parent=self,
        ):
            return
        try:
            self.app.database.set_metadata_value(
                LEGACY_MIGRATION_DECLINED_KEY,
                "1",
            )
        except Exception as exc:
            self.app.logger.error(
                "legacy_migration_decline_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Nuova installazione",
                "Impossibile registrare in sicurezza la scelta. "
                "Nessun dato precedente è stato modificato.",
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
        """Build the operator-first 5.0 workspace.

        Controller credentials deliberately live only on the Settings page.
        Home and Voucher remain focused on the operator's daily work.
        """

        self.apply_theme()
        self._configure_style()
        self.after_idle(self._maximize_window)

        shell = ttk.Frame(self)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(shell, padding=(18, 20))
        sidebar.grid(row=0, column=0, sticky="ns")
        ttk.Label(
            sidebar,
            text=PRODUCT_NAME,
            style="SidebarTitle.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            sidebar,
            text="Gestione voucher Wi-Fi",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 24))

        self._page_frames = {}
        self._nav_buttons = {}
        nav_items = (
            ("home", "Home"),
            ("voucher", "Voucher"),
            ("report", "Report"),
            ("settings", "Impostazioni"),
        )
        for key, label in nav_items:
            button = ttk.Button(
                sidebar,
                text=label,
                command=lambda page=key: self._show_page(page),
                style="Nav.TButton",
                width=20,
            )
            button.pack(fill="x", pady=3)
            self._nav_buttons[key] = button

        ttk.Separator(sidebar).pack(fill="x", pady=(22, 14))
        self.sidebar_status_var = tk.StringVar(value="●  Non collegato")
        self.sidebar_status_label = ttk.Label(
            sidebar,
            textvariable=self.sidebar_status_var,
            style="ConnectionOffline.TLabel",
            wraplength=180,
        )
        self.sidebar_status_label.pack(anchor="w")
        ttk.Label(
            sidebar,
            text="Le credenziali del controller non vengono salvate.",
            style="Muted.TLabel",
            wraplength=180,
        ).pack(anchor="w", pady=(6, 0))

        workspace = ttk.Frame(shell, padding=(26, 20))
        workspace.grid(row=0, column=1, sticky="nsew")
        workspace.columnconfigure(0, weight=1)
        workspace.rowconfigure(0, weight=1)

        self.page_host = ttk.Frame(workspace)
        self.page_host.grid(row=0, column=0, sticky="nsew")
        self.page_host.columnconfigure(0, weight=1)
        self.page_host.rowconfigure(0, weight=1)

        for key, _label in nav_items:
            page = ttk.Frame(self.page_host)
            page.grid(row=0, column=0, sticky="nsew")
            self._page_frames[key] = page

        self._build_home_page(self._page_frames["home"])
        self._build_voucher_page(self._page_frames["voucher"])
        self._build_report_page(self._page_frames["report"])
        self._build_settings_page(self._page_frames["settings"])

        activity = ttk.Frame(workspace)
        activity.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        activity.columnconfigure(0, weight=1)
        self.background_operation_var = tk.StringVar()
        self.background_progress = ttk.Progressbar(
            activity,
            mode="indeterminate",
        )
        self.background_progress.grid(row=0, column=0, sticky="ew")
        self.background_progress.grid_remove()
        self.background_operation_label = ttk.Label(
            activity,
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

        self._show_page("home")

    def _page_heading(
        self,
        parent: ttk.Frame,
        title: str,
        subtitle: str,
    ) -> ttk.Frame:
        header = ttk.Frame(parent)
        header.pack(fill="x", pady=(0, 20))
        ttk.Label(
            header,
            text=title,
            style="PageTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            header,
            text=subtitle,
            style="Muted.TLabel",
            wraplength=900,
        ).pack(anchor="w", pady=(4, 0))
        return header

    def _build_home_page(self, page: ttk.Frame) -> None:
        self._page_heading(
            page,
            "Home",
            "Stato della postazione e attività quotidiane.",
        )

        status = ttk.Frame(page, padding=18, style="Card.TFrame")
        status.pack(fill="x", pady=(0, 14))
        status.columnconfigure(1, weight=1)
        self.home_status_var = tk.StringVar(value="Non collegato")
        self.home_status_detail_var = tk.StringVar(
            value="Configura o collega il controller da Impostazioni."
        )
        self.home_status_label = ttk.Label(
            status,
            textvariable=self.home_status_var,
            style="StatusOffline.TLabel",
        )
        self.home_status_label.grid(row=0, column=0, sticky="w")
        ttk.Label(
            status,
            textvariable=self.home_status_detail_var,
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(
            status,
            text="Controller",
            command=lambda: self._show_page("settings"),
        ).grid(row=0, column=1, sticky="e")

        cards = ttk.Frame(page)
        cards.pack(fill="x", pady=(0, 18))
        for column in range(4):
            cards.columnconfigure(column, weight=1)

        self.home_active_var = tk.StringVar(value="0")
        self.home_queue_var = tk.StringVar(value="0")
        self.home_used_var = tk.StringVar(value="0")
        self.home_printed_var = tk.StringVar(value="0")
        card_data = (
            ("Voucher attivi", self.home_active_var),
            ("Da stampare", self.home_queue_var),
            ("Utilizzati", self.home_used_var),
            ("Già stampati", self.home_printed_var),
        )
        for column, (label, variable) in enumerate(card_data):
            card = ttk.Frame(cards, padding=16, style="Card.TFrame")
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
                text=label,
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(3, 0))

        ttk.Label(
            page,
            text="Azioni rapide",
            style="SectionTitle.TLabel",
        ).pack(anchor="w", pady=(0, 8))
        actions = ttk.Frame(page)
        actions.pack(fill="x")
        ttk.Button(
            actions,
            text="＋  Nuovo voucher",
            command=self.create,
            style="Hero.TButton",
        ).pack(side="left")
        ttk.Button(
            actions,
            text="Voucher da stampare",
            command=self._open_print_queue,
        ).pack(side="left", padx=8)
        ttk.Button(
            actions,
            text="Report",
            command=lambda: self._show_page("report"),
        ).pack(side="left")

        ttk.Separator(page).pack(fill="x", pady=22)
        ttk.Label(
            page,
            text="Attività",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        self.home_activity_var = tk.StringVar(
            value="Nessuna attività nella sessione corrente."
        )
        ttk.Label(
            page,
            textvariable=self.home_activity_var,
            style="Muted.TLabel",
            wraplength=900,
        ).pack(anchor="w", pady=(6, 0))

    def _build_voucher_page(self, page: ttk.Frame) -> None:
        self._page_heading(
            page,
            "Voucher",
            "Crea, seleziona, prepara e stampa i voucher per gli ospiti.",
        )

        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(0, 14))
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
        self.print_button.pack(side="left", padx=(10, 18))
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

        filters = ttk.Frame(page, padding=(0, 2))
        filters.pack(fill="x", pady=(0, 10))
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
            width=14,
        )
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self.populate())
        ttk.Label(
            filters,
            text="Cerca",
            style="Muted.TLabel",
        ).pack(side="left", padx=(20, 7))
        search = ttk.Entry(filters, textvariable=self.search_var, width=34)
        search.pack(side="left")
        self._search_after = None
        search.bind("<KeyRelease>", self._schedule_search_populate)
        ttk.Label(
            filters,
            textvariable=self.count_var,
            style="Muted.TLabel",
        ).pack(side="right")

        table = ttk.Frame(page)
        table.pack(fill="both", expand=True)
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
        self.tree.heading("check", text="☐", command=self.toggle_all_visible)
        self.tree.column(
            "check",
            width=42,
            anchor="center",
            stretch=False,
        )
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
        sy = ttk.Scrollbar(
            table,
            orient="vertical",
            command=self.tree.yview,
        )
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        ttk.Label(
            page,
            text=(
                "La selezione resta evidenziata fino alla stampa confermata. "
                "Una ristampa richiede una conferma esplicita."
            ),
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    def _build_report_page(self, page: ttk.Frame) -> None:
        self._page_heading(
            page,
            "Report",
            "Consulta ed esporta i dati operativi senza esporre i codici voucher.",
        )
        intro = ttk.Frame(page, padding=18, style="Card.TFrame")
        intro.pack(fill="x", pady=(0, 16))
        ttk.Label(
            intro,
            text="Reporting amministrativo",
            style="SectionTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            intro,
            text=(
                "Genera riepiloghi, voucher utilizzati, scaduti, stampati "
                "e viste storiche in PDF o CSV."
            ),
            style="Muted.TLabel",
            wraplength=820,
        ).pack(anchor="w", pady=(4, 12))
        self.report_button = ttk.Button(
            intro,
            text="Apri generatore report…",
            command=lambda: ReportDialog(self),
            style="Accent.TButton",
        )
        self.report_button.pack(anchor="w")

        shortcuts = ttk.Frame(page)
        shortcuts.pack(fill="x", pady=(0, 18))
        for column in range(4):
            shortcuts.columnconfigure(column, weight=1)
        quick_reports = (
            ("Riepilogo", "Riepilogo"),
            ("Utilizzati", "Voucher utilizzati"),
            ("Scaduti", "Voucher scaduti"),
            ("Stampati non usati", "Stampati mai utilizzati"),
        )
        for column, (title, kind) in enumerate(quick_reports):
            card = ttk.Frame(shortcuts, padding=14, style="Card.TFrame")
            card.grid(
                row=0,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 6, 0 if column == 3 else 6),
            )
            ttk.Label(
                card,
                text=title,
                style="SectionTitle.TLabel",
            ).pack(anchor="w")
            ttk.Button(
                card,
                text="Apri…",
                command=lambda selected=kind: ReportDialog(
                    self,
                    initial_kind=selected,
                ),
            ).pack(anchor="w", pady=(10, 0))

        ttk.Label(
            page,
            text="Stato corrente",
            style="SectionTitle.TLabel",
        ).pack(anchor="w", pady=(8, 8))
        self.report_summary_var = tk.StringVar(value="Nessun dato disponibile.")
        ttk.Label(
            page,
            textvariable=self.report_summary_var,
            style="Muted.TLabel",
            wraplength=900,
        ).pack(anchor="w")

    def _build_settings_page(self, page: ttk.Frame) -> None:
        self._page_heading(
            page,
            "Impostazioni",
            "Configurazione dell'applicazione, controller, stampa, retention e backup.",
        )

        if not hasattr(self, "controller_profile_name_var"):
            current_name = (
                self.database.controller_name(self.active_controller_id)
                if self.active_controller_id is not None
                else None
            )
            self.controller_profile_name_var = tk.StringVar(
                value=current_name or "Controller UniFi"
            )

        controller = ttk.Frame(page, padding=16, style="Card.TFrame")
        controller.pack(fill="x", pady=(0, 12))
        ttk.Label(
            controller,
            text="Controller",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(
            controller,
            text=(
                "La API key viene usata solo per questa connessione e non viene "
                "mai memorizzata."
            ),
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(3, 10))
        ttk.Label(controller, text="Nome profilo").grid(
            row=2, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Entry(
            controller,
            textvariable=self.controller_profile_name_var,
        ).grid(row=2, column=1, columnspan=3, sticky="ew", padx=(0, 12))

        ttk.Label(controller, text="API root").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        self.api_root_entry = ttk.Entry(
            controller,
            textvariable=self.api_root_var,
        )
        self.api_root_entry.grid(
            row=3, column=1, sticky="ew", padx=(0, 16), pady=(8, 0)
        )
        self.api_root_entry.bind("<Return>", lambda _event: self.connect())
        ttk.Label(controller, text="API key").grid(
            row=3, column=2, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        self.api_key_entry = ttk.Entry(
            controller,
            textvariable=self.api_key_var,
            show="•",
            width=28,
        )
        self.api_key_entry.grid(
            row=3, column=3, sticky="ew", padx=(0, 12), pady=(8, 0)
        )
        self.api_key_entry.bind("<Return>", lambda _event: self.connect())
        self.connect_button = ttk.Button(
            controller,
            text="Verifica / Connetti",
            command=self.connect,
            style="Accent.TButton",
        )
        self.connect_button.grid(row=3, column=4, sticky="e", pady=(8, 0))
        ttk.Label(
            controller,
            textvariable=self.connection_var,
            style="ConnectionStatus.TLabel",
        ).grid(
            row=4,
            column=0,
            columnspan=5,
            sticky="w",
            pady=(10, 0),
        )
        controller.columnconfigure(1, weight=1)
        controller.columnconfigure(3, weight=1)

        sections = ttk.Frame(page)
        sections.pack(fill="x")
        for column in range(4):
            sections.columnconfigure(column, weight=1)

        cards = (
            (
                "Generali",
                "Identità della postazione e tema Windows.",
                lambda: SettingsDialog(self, initial_tab="Generali"),
            ),
            (
                "PDF / stampa",
                "Logo, aspetto voucher e archivio PDF.",
                lambda: SettingsDialog(self, initial_tab="PDF / stampa"),
            ),
            (
                "Retention",
                "Conservazione e revisione dei voucher storici.",
                lambda: SettingsDialog(self, initial_tab="Retention"),
            ),
            (
                "Backup",
                "Backup, ripristino e strumenti dati avanzati.",
                lambda: SettingsDialog(self, initial_tab="Backup"),
            ),
        )
        for column, (title, detail, command) in enumerate(cards):
            card = ttk.Frame(sections, padding=16, style="Card.TFrame")
            card.grid(
                row=0,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 6, 0 if column == 3 else 6),
            )
            ttk.Label(
                card,
                text=title,
                style="SectionTitle.TLabel",
            ).pack(anchor="w")
            ttk.Label(
                card,
                text=detail,
                style="Muted.TLabel",
                wraplength=210,
            ).pack(anchor="w", pady=(4, 12))
            ttk.Button(
                card,
                text="Apri…",
                command=command,
            ).pack(anchor="w")

    def select_controller_profile(self, profile: dict) -> None:
        """Switch the operator workspace to one saved local controller profile."""

        controller_id = int(profile["id"])
        api_root = str(profile.get("api_root") or "").strip()
        name = str(profile.get("name") or "Controller UniFi").strip()
        cert_sha256 = str(profile.get("cert_sha256") or "").strip()

        if self.client is not None and self.client.base_url != api_root:
            self.client = None
        self.active_controller_id = controller_id
        self.api_root_var.set(api_root)
        self.controller_profile_name_var.set(name)
        self.api_key_var.set("")
        self.settings = self.settings_store.update(
            controller_api_root=api_root,
            controller_cert_sha256=cert_sha256,
        )
        self.vouchers = load_local_vouchers(
            self.database,
            controller_id=controller_id,
        )
        self.checked_ids.clear()
        if self.client is not None:
            self.connection_var.set(f"Connesso • {name}")
        else:
            self.connection_var.set(f"Modalità locale • {name}")
        self.populate()
        self._refresh_workspace_summary()

    def clear_active_controller_profile(self) -> None:
        """Clear current controller selection without deleting historical data."""

        self.client = None
        self.active_controller_id = None
        self.api_root_var.set("")
        self.api_key_var.set("")
        self.controller_profile_name_var.set("Controller UniFi")
        self.settings = self.settings_store.update(
            controller_api_root="",
            controller_cert_sha256="",
        )
        self.vouchers = []
        self.checked_ids.clear()
        self.connection_var.set("Non connesso")
        self.populate()
        self._refresh_workspace_summary()

    def _show_page(self, page: str) -> None:
        frame = self._page_frames.get(page)
        if frame is None:
            return
        frame.tkraise()
        self._active_page = page
        self._refresh_workspace_summary()

    def _open_print_queue(self) -> None:
        self.filter_var.set("Da stampare")
        self._show_page("voucher")
        self.populate()

    def _refresh_workspace_summary(self, stats=None) -> None:
        connected = self.client is not None
        local = self.active_controller_id is not None
        if connected:
            status = "●  Pronto"
            detail = "Controller collegato. La postazione è pronta."
            sidebar_style = "ConnectionReady.TLabel"
            home_style = "StatusReady.TLabel"
        elif local:
            status = "●  Modalità locale"
            detail = (
                "Dati locali disponibili. Collega il controller da "
                "Impostazioni per sincronizzare."
            )
            sidebar_style = "ConnectionLocal.TLabel"
            home_style = "StatusLocal.TLabel"
        else:
            status = "●  Non collegato"
            detail = "Configura o collega il controller da Impostazioni."
            sidebar_style = "ConnectionOffline.TLabel"
            home_style = "StatusOffline.TLabel"

        if hasattr(self, "sidebar_status_var"):
            self.sidebar_status_var.set(status)
        if hasattr(self, "sidebar_status_label"):
            self.sidebar_status_label.configure(style=sidebar_style)
        if hasattr(self, "home_status_var"):
            self.home_status_var.set(status.replace("●  ", ""))
        if hasattr(self, "home_status_label"):
            self.home_status_label.configure(style=home_style)
        if hasattr(self, "home_status_detail_var"):
            self.home_status_detail_var.set(detail)

        if stats is None:
            try:
                stats = self.history.stats_for_codes(
                    [v.code_formatted for v in self.vouchers],
                    self.settings,
                )
            except Exception:
                stats = {}

        active = [
            voucher
            for voucher in self.vouchers
            if not self._is_expired(voucher)
        ]
        used = sum(1 for voucher in active if int(voucher.used or 0) > 0)
        printed = 0
        queue = 0
        for voucher in active:
            state = self._print_state(stats.get(voucher.code_formatted))
            if state == "STAMPATO":
                printed += 1
            else:
                queue += 1

        if hasattr(self, "home_active_var"):
            self.home_active_var.set(str(len(active)))
            self.home_queue_var.set(str(queue))
            self.home_used_var.set(str(used))
            self.home_printed_var.set(str(printed))
            self.report_summary_var.set(
                f"{len(active)} voucher attivi • {used} utilizzati • "
                f"{printed} stampati • {queue} ancora da stampare"
            )
            try:
                recent = self.database.recent_operator_activity(limit=5)
            except Exception as exc:
                self.logger.debug(
                    "home_recent_activity_unavailable type=%s",
                    type(exc).__name__,
                )
                recent = []

            activity_labels = {
                ("PRINT", "AUDITED"): "Stampa registrata",
                ("PRINT", "SUBMITTED"): "Stampa inviata",
                ("PRINT", "PREPARED"): "Stampa in preparazione",
                ("PRINT", "UNCERTAIN"): "Stampa da verificare",
                ("BACKUP", "SUCCESS"): "Backup completato",
                ("BACKUP", "FAILED"): "Backup non riuscito",
                ("VOUCHER", "RETENTION_ARCHIVED"): "Dati voucher minimizzati",
                ("VOUCHER", "LEGACY_PDF_GENERATED"): "Documento storico importato",
            }
            activity_lines = []
            for item in recent:
                key = (
                    str(item.get("kind") or ""),
                    str(item.get("status") or ""),
                )
                label = activity_labels.get(
                    key,
                    "Attività voucher"
                    if key[0] == "VOUCHER"
                    else (
                        "Attività di stampa"
                        if key[0] == "PRINT"
                        else "Attività backup"
                    ),
                )
                stamp = audit_time_label(str(item.get("occurred_at") or ""))
                activity_lines.append(f"{stamp}  •  {label}")

            self.home_activity_var.set(
                "\n".join(activity_lines)
                if activity_lines
                else "Nessuna attività registrata."
            )

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
            self.report_button,
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
        style.configure("PageTitle.TLabel", font=("Segoe UI Variable Display", 24, "bold"))
        style.configure("SidebarTitle.TLabel", font=("Segoe UI Variable Display", 15, "bold"))
        style.configure("SectionTitle.TLabel", font=("Segoe UI Variable Text", 11, "bold"))
        style.configure("StatusTitle.TLabel", font=("Segoe UI Variable Text", 13, "bold"))
        style.configure("Metric.TLabel", font=("Segoe UI Variable Display", 24, "bold"))
        style.configure("Muted.TLabel", font=("Segoe UI Variable Text", 9))
        style.configure("ConnectionStatus.TLabel", font=("Segoe UI Variable Text", 10, "bold"))
        style.configure("ConnectionReady.TLabel", font=("Segoe UI Variable Text", 10, "bold"), foreground="#107c10")
        style.configure("ConnectionLocal.TLabel", font=("Segoe UI Variable Text", 10, "bold"), foreground="#9a6700")
        style.configure("ConnectionOffline.TLabel", font=("Segoe UI Variable Text", 10, "bold"), foreground="#c42b1c")
        style.configure("StatusReady.TLabel", font=("Segoe UI Variable Text", 13, "bold"), foreground="#107c10")
        style.configure("StatusLocal.TLabel", font=("Segoe UI Variable Text", 13, "bold"), foreground="#9a6700")
        style.configure("StatusOffline.TLabel", font=("Segoe UI Variable Text", 13, "bold"), foreground="#c42b1c")
        style.configure("Card.TFrame", padding=2)
        style.configure("Nav.TButton", anchor="w", padding=(14, 10))
        style.configure("Hero.TButton", font=("Segoe UI Variable Text", 10, "bold"), padding=(20, 11))
        style.configure("Treeview", rowheight=38, font=("Segoe UI Variable Text", 10))
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
            if voucher.id in self.checked_ids and not expired:
                self.tree.selection_add(iid)
        self._refresh_workspace_summary(stats)
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
