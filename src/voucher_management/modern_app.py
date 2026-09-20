"""Windows operator interface for Voucher Management."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import darkdetect
import sv_ttk

from .app import VoucherApp, duration_label, time_label
from .backup import BackupError, BackupService
from .history import HistoryError
from .identity import (
    DEFAULT_STRUCTURE_NAME,
    DEFAULT_STRUCTURE_TYPE,
    DEFAULT_WIFI_TITLE,
    PRODUCT_NAME,
)
from .pdf_render import VOUCHERS_PER_PAGE
from .policy import evaluate_delete_policy
from .unifi_api import (
    UniFiApiError,
    UniFiCertificateChanged,
    UniFiCertificateTrustRequired,
    UniFiClient,
    normalize_api_root,
)


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
        ttk.Label(frame, text="Backup e ripristino", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Il backup comprende configurazione, storico, PDF generati, loghi e la chiave portabile della cronologia. La API key UniFi non viene mai salvata.", style="Muted.TLabel", wraplength=560).pack(anchor="w", pady=(3, 12))
        actions = ttk.Frame(frame)
        actions.pack(anchor="w")
        ttk.Button(actions, text="Crea backup…", command=self.app.create_backup).pack(side="left")
        ttk.Button(actions, text="Ripristina backup…", command=self.app.restore_backup).pack(side="left", padx=(8, 0))

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
        old_theme = self.app.settings.get("ui_theme", "system")
        self.app.settings = self.app.settings_store.update(
            structure_type=self.structure_type.get(),
            structure_name=self.structure_name.get().strip(),
            wifi_title=self.wifi_title.get().strip(),
            preset=self.preset.get(),
            logo_path=self.logo.get().strip(),
            ui_theme=self.theme.get(),
        )
        if old_theme != self.theme.get():
            self.app.apply_theme()
        self.destroy()


class ModernVoucherApp(VoucherApp):
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
        api_root_entry = ttk.Entry(
            connection,
            textvariable=self.api_root_var,
            width=46,
        )
        api_root_entry.grid(row=0, column=1, sticky="ew", padx=(0, 16))
        api_root_entry.bind("<Return>", lambda _event: self.connect())

        ttk.Label(connection, text="API key", style="Muted.TLabel").grid(
            row=0, column=2, sticky="w", padx=(0, 6)
        )
        api_key_entry = ttk.Entry(
            connection,
            textvariable=self.api_key_var,
            show="•",
            width=32,
        )
        api_key_entry.grid(row=0, column=3, sticky="ew", padx=(0, 16))
        api_key_entry.bind("<Return>", lambda _event: self.connect())

        ttk.Button(
            connection,
            text="Connetti",
            command=self.connect,
            style="Accent.TButton",
        ).grid(row=0, column=4)

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

        connection.columnconfigure(1, weight=1)
        connection.columnconfigure(3, weight=1)

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(2, 12))
        ttk.Button(actions, text="＋  NUOVO VOUCHER", command=self.create, style="Hero.TButton").pack(side="left")
        ttk.Button(actions, textvariable=self.action_var, command=self.print_selected, style="Hero.TButton").pack(side="left", padx=(10, 22))
        ttk.Button(actions, text="Seleziona da stampare", command=self.select_unprinted).pack(side="left")
        ttk.Button(actions, text="Aggiorna", command=self.refresh).pack(side="left", padx=8)
        ttk.Button(actions, text="Apri PDF", command=self.open_existing_pdf).pack(side="left")
        ttk.Button(actions, text="Elimina", command=self.delete_selected).pack(side="right")

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

    def _backup_service(self) -> BackupService:
        return BackupService(self.paths)

    def create_backup(self) -> None:
        default = f"VoucherManagement-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
        target = filedialog.asksaveasfilename(parent=self, title="Crea backup", defaultextension=".zip", initialfile=default, filetypes=[("Backup Voucher Management", "*.zip")])
        if not target:
            return
        try:
            result = self._backup_service().create(Path(target))
            messagebox.showinfo("Backup completato", f"Backup creato correttamente.\n\n{result}", parent=self)
        except BackupError as exc:
            messagebox.showerror("Backup", str(exc), parent=self)

    def restore_backup(self) -> None:
        source = filedialog.askopenfilename(parent=self, title="Ripristina backup", filetypes=[("Backup Voucher Management", "*.zip")])
        if not source:
            return
        try:
            manifest = self._backup_service().validate(Path(source))
        except BackupError as exc:
            messagebox.showerror("Ripristino", str(exc), parent=self)
            return
        created = manifest.get("created_utc", "data sconosciuta")
        if not messagebox.askyesno("Ripristina backup", f"Ripristinare il backup creato il {created}?\n\nPrima della sostituzione verrà conservata automaticamente una copia di rollback dei dati attuali.\n\nDopo il ripristino il programma verrà chiuso.", parent=self):
            return
        try:
            rollback = self._backup_service().restore(Path(source))
            messagebox.showinfo("Ripristino completato", f"Dati ripristinati.\n\nCopia di sicurezza precedente:\n{rollback}\n\nRiavviare Voucher Management.", parent=self)
            self.destroy()
        except BackupError as exc:
            messagebox.showerror("Ripristino", str(exc), parent=self)

    @staticmethod
    def _format_certificate_fingerprint(value: str) -> str:
        compact = value.replace(":", "").strip().upper()
        return ":".join(
            compact[index:index + 2]
            for index in range(0, len(compact), 2)
        )

    def connect(self) -> None:
        """Connect through the official API without persisting the API key."""

        api_root = self.api_root_var.get().strip()
        api_key = self.api_key_var.get()
        self.connection_var.set("Connessione in corso…")
        self.update_idletasks()

        try:
            normalized = normalize_api_root(api_root)
            # Always resolve persisted trust from the latest on-disk state.
            self.settings = self.settings_store.load()
            saved_root = str(self.settings.get("controller_api_root", "")).strip()
            saved_pin = str(self.settings.get("controller_cert_sha256", "")).strip()
            trusted_pin = saved_pin if saved_root == normalized else ""
            client = UniFiClient(
                normalized,
                trusted_cert_sha256=trusted_pin or None,
            )
            try:
                info = client.connect(api_key)
                vouchers = client.list_vouchers()
            except UniFiCertificateChanged as exc:
                previous = self._format_certificate_fingerprint(
                    exc.previous_fingerprint
                )
                current = self._format_certificate_fingerprint(
                    exc.fingerprint
                )
                accepted = messagebox.askyesno(
                    "Certificato TLS cambiato",
                    "Il certificato del controller non corrisponde più "
                    "all'impronta autorizzata.\n\n"
                    f"Impronta precedente:\n{previous}\n\n"
                    f"Nuova impronta:\n{current}\n\n"
                    "La API key non è stata inviata al controller. "
                    "Verificare la nuova impronta tramite una fonte attendibile "
                    "prima di continuare.\n\n"
                    "Sostituire l'impronta memorizzata e connettersi?",
                    parent=self,
                )
                if not accepted:
                    raise UniFiApiError(
                        "Nuovo certificato TLS non autorizzato dall'operatore"
                    )
                client = UniFiClient(
                    normalized,
                    trusted_cert_sha256=exc.fingerprint,
                )
                info = client.connect(api_key)
                vouchers = client.list_vouchers()
            except UniFiCertificateTrustRequired as exc:
                formatted = self._format_certificate_fingerprint(
                    exc.fingerprint
                )
                accepted = messagebox.askyesno(
                    "Certificato TLS non attendibile",
                    "Il controller usa un certificato che Windows non considera "
                    "attendibile.\n\nImpronta SHA-256:\n"
                    f"{formatted}\n\n"
                    "Confermare solo dopo aver verificato che l'impronta "
                    "appartenga realmente al controller.\n\n"
                    "Memorizzare e autorizzare questo certificato?",
                    parent=self,
                )
                if not accepted:
                    raise UniFiApiError(
                        "Certificato TLS non autorizzato dall'operatore"
                    )
                client = UniFiClient(
                    normalized,
                    trusted_cert_sha256=exc.fingerprint,
                )
                info = client.connect(api_key)
                vouchers = client.list_vouchers()
        except (UniFiApiError, ValueError) as exc:
            self.client = None
            self.api_key_var.set("")
            self.connection_var.set("Connessione non riuscita")
            messagebox.showerror("UniFi", str(exc), parent=self)
            return

        self.api_key_var.set("")
        self.client = client
        self.vouchers = vouchers

        self.api_root_var.set(client.base_url)
        self.settings = self.settings_store.update(
            controller_api_root=client.base_url,
            controller_cert_sha256=client.trusted_cert_sha256,
        )

        tls_label = (
            "TLS certificato fissato"
            if client.trusted_cert_sha256
            else "TLS verificato"
        )
        site_label = info.get("siteName") or "sito"
        self.connection_var.set(
            f"Connesso • Network {info['applicationVersion']} • "
            f"{site_label} • {tls_label}"
        )
        self.checked_ids.clear()
        self.populate()
        self.logger.info(
            "controller_api_connected network_version=%s tls_pinned=%s",
            info["applicationVersion"],
            bool(client.trusted_cert_sha256),
        )

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
                    "per evitare decisioni basate su dati incompleti.",
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

    def delete_selected(self) -> None:
        """Delete only vouchers that are still unused at deletion time."""
        selected = self.selected()
        if not selected:
            messagebox.showinfo(
                "Elimina da UniFi",
                "Selezionare uno o più voucher attivi.",
                parent=self,
            )
            return

        # The table is only a cache. Re-read every selected voucher immediately
        # before applying lifecycle policy so a guest authorized since the last
        # refresh can never be revoked from stale UI state.
        try:
            current = [
                self.client.get_voucher(voucher.id)
                for voucher in selected
            ]
        except UniFiApiError as exc:
            messagebox.showerror(
                "Eliminazione",
                "Impossibile verificare lo stato aggiornato dei voucher. "
                f"Nessun voucher è stato eliminato.\n\n{exc}",
                parent=self,
            )
            return

        stats = self._history_stats_for(current)
        if stats is None:
            return

        blocked = []
        for voucher in current:
            result = evaluate_delete_policy(
                voucher,
                stats.get(voucher.code_formatted),
            )
            if not result.allowed:
                blocked.append((voucher, result))
        if blocked:
            reasons = {result.reason for _voucher, result in blocked}
            if "in_use" in reasons:
                detail = (
                    "Almeno un voucher selezionato risulta già utilizzato o "
                    "in uso sul controller."
                )
            elif "printed" in reasons:
                detail = (
                    "Almeno un voucher selezionato risulta già stampato."
                )
            else:
                detail = (
                    "Almeno un voucher selezionato non è eliminabile "
                    "dall'applicazione."
                )
            messagebox.showwarning(
                "Eliminazione non consentita",
                f"{detail}\n\nVoucher Management consente solo la pulizia "
                "dei voucher non ancora emessi. L'eventuale revoca resta di "
                "competenza dell'amministratore IT.",
                parent=self,
            )
            return

        if not messagebox.askyesno(
            "Elimina dal server UniFi",
            f"Eliminare {len(current)} voucher dal server UniFi?\n\n"
            "Questa operazione rimuove i voucher dal controller. Lo storico "
            "locale e gli eventuali PDF già generati non verranno cancellati.",
            parent=self,
        ):
            return

        try:
            self.client.delete_vouchers([v.id for v in current])
        except UniFiApiError as exc:
            # A multi-delete can partially complete. Refresh best-effort so the
            # UI does not encourage a second action against stale rows.
            try:
                self.vouchers = self.client.list_vouchers()
                self.checked_ids.clear()
                self.populate()
            except UniFiApiError:
                pass
            messagebox.showerror("Eliminazione", str(exc), parent=self)
            return

        self.checked_ids.clear()
        try:
            self.vouchers = self.client.list_vouchers()
        except UniFiApiError as exc:
            deleted_ids = {voucher.id for voucher in current}
            self.vouchers = [
                voucher for voucher in self.vouchers
                if voucher.id not in deleted_ids
            ]
            self.populate()
            messagebox.showwarning(
                "Eliminazione completata",
                f"Eliminati {len(current)} voucher, ma l'aggiornamento "
                f"dell'elenco non è riuscito.\n\n{exc}",
                parent=self,
            )
            return

        self.populate()
        messagebox.showinfo(
            "Eliminazione",
            f"Eliminati {len(current)} voucher dal server UniFi.",
            parent=self,
        )


def main() -> int:
    logging.getLogger("PIL").setLevel(logging.WARNING)
    app = ModernVoucherApp()
    if app.winfo_exists():
        app.mainloop()
    return 0
