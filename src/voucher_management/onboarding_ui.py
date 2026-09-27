"""First-run onboarding wizard for genuinely new 5.0 installations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .controller_probe import ControllerProbeResult, probe_controller
from .identity import (
    DEFAULT_STRUCTURE_NAME,
    DEFAULT_STRUCTURE_TYPE,
    DEFAULT_WIFI_TITLE,
)
from .logo_validation import LogoValidationError, validate_logo_image
from .shared_data_migration import source_has_migratable_data
from .onboarding import (
    DEFAULT_VOUCHER_RETENTION_DAYS,
    ONBOARDING_IN_PROGRESS_KEY,
    OnboardingDraft,
    OnboardingState,
    begin_onboarding,
    choose_shared_fresh_start,
    complete_onboarding,
    legacy_installation_has_evidence,
    onboarding_state,
    shared_fresh_start_selected,
)
from .unifi_api import (
    UniFiApiError,
    UniFiCertificateChanged,
    UniFiCertificateTrustRequired,
    normalize_api_root,
)


def _format_fingerprint(value: str) -> str:
    compact = str(value or "").replace(":", "").strip().upper()
    return ":".join(
        compact[index:index + 2]
        for index in range(0, len(compact), 2)
    )


def startup_onboarding_state(app) -> OnboardingState:
    """Return the startup disposition before scheduling any modal UI."""

    state = onboarding_state(app.database)
    if state is not OnboardingState.REQUIRED:
        return state

    # Once onboarding has begun, its durable retry marker wins over filesystem
    # evidence produced by the partial attempt itself.
    if app.database.metadata_value(ONBOARDING_IN_PROGRESS_KEY) == "1":
        return OnboardingState.REQUIRED

    if legacy_installation_has_evidence(
        app.paths,
        getattr(app, "settings", {}),
    ):
        return OnboardingState.EXISTING_INSTALLATION

    if (
        getattr(app.paths, "shared_mode", False)
        and not shared_fresh_start_selected(app.database)
        and source_has_migratable_data(app.paths.per_user_root)
    ):
        # Shared ProgramData must remain pristine until the explicit per-user
        # migration has had a chance to run. Starting onboarding here would
        # write app_metadata/profile rows and correctly make migration refuse
        # to overwrite the target.
        return OnboardingState.MIGRATION_AVAILABLE
    return OnboardingState.REQUIRED


def schedule_first_run_onboarding(
    app,
    *,
    wizard_factory=None,
) -> OnboardingState:
    """Schedule the wizard only for a genuinely new/incomplete installation."""

    state = startup_onboarding_state(app)
    if state is OnboardingState.REQUIRED:
        factory = wizard_factory or FirstRunWizard
        app.after_idle(lambda: factory(app))
    return state


class FirstRunWizard(tk.Toplevel):
    """Mandatory first-run setup for a fresh database only."""

    PAGE_WELCOME = 0
    PAGE_IDENTITY = 1
    PAGE_CONTROLLER = 2
    PAGE_RETENTION = 3
    PAGE_SUMMARY = 4
    LAST_PAGE = PAGE_SUMMARY

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Prima configurazione")
        self.transient(app)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        try:
            begin_onboarding(app.database)
        except Exception as exc:
            app.logger.error(
                "onboarding_begin_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Prima configurazione",
                "Impossibile inizializzare in sicurezza la prima configurazione.",
                parent=self,
            )
            self.destroy()
            app.destroy()
            return

        settings = app.settings
        default_structure = (
            str(settings.get("structure_name", "")).strip()
            or DEFAULT_STRUCTURE_NAME
        )
        self.page = self.PAGE_WELCOME
        self._controller_result: ControllerProbeResult | None = None
        self._verified_root = ""

        self.installation_name_var = tk.StringVar(
            value=default_structure or "Postazione Voucher Management"
        )
        self.description_var = tk.StringVar()
        self.structure_type_var = tk.StringVar(
            value=str(
                settings.get("structure_type", DEFAULT_STRUCTURE_TYPE)
                or DEFAULT_STRUCTURE_TYPE
            )
        )
        self.structure_name_var = tk.StringVar(value=default_structure)
        self.wifi_title_var = tk.StringVar(
            value=str(
                settings.get("wifi_title", DEFAULT_WIFI_TITLE)
                or DEFAULT_WIFI_TITLE
            )
        )
        self.logo_var = tk.StringVar(
            value=str(settings.get("logo_path", "") or "")
        )

        self.controller_name_var = tk.StringVar(value="Controller UniFi")
        self.controller_root_var = tk.StringVar(
            value=str(settings.get("controller_api_root", "") or "")
        )
        self.api_key_var = tk.StringVar()
        self.controller_status_var = tk.StringVar(
            value="Connessione non ancora verificata"
        )

        self.retention_days_var = tk.StringVar(
            value=str(DEFAULT_VOUCHER_RETENTION_DAYS)
        )

        shell = ttk.Frame(self, padding=22)
        shell.pack(fill="both", expand=True)
        self.header_var = tk.StringVar()
        ttk.Label(
            shell,
            textvariable=self.header_var,
            style="PageTitle.TLabel",
        ).pack(anchor="w")
        self.subtitle_var = tk.StringVar()
        ttk.Label(
            shell,
            textvariable=self.subtitle_var,
            style="Muted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(3, 16))

        self.body = ttk.Frame(shell)
        self.body.pack(fill="both", expand=True)

        ttk.Separator(shell).pack(fill="x", pady=(18, 12))
        footer = ttk.Frame(shell)
        footer.pack(fill="x")
        self.cancel_button = ttk.Button(
            footer,
            text="Annulla",
            command=self._cancel,
        )
        self.cancel_button.pack(side="left")
        self.next_button = ttk.Button(
            footer,
            text="Continua",
            command=self._next,
            style="Accent.TButton",
            width=18,
        )
        self.next_button.pack(side="right")
        self.back_button = ttk.Button(
            footer,
            text="Indietro",
            command=self._back,
            width=12,
        )
        self.back_button.pack(side="right", padx=(0, 8))

        self._render_page()
        self._fit()

    def _fit(self) -> None:
        self.update_idletasks()
        width = max(730, self.winfo_reqwidth() + 30)
        height = max(540, self.winfo_reqheight() + 30)
        width = min(width, max(500, self.winfo_screenwidth() - 80))
        height = min(height, max(420, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(min(width, 760), min(height, 560))

    def _clear_body(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def _render_page(self) -> None:
        self._clear_body()
        self.back_button.state(
            ["disabled"] if self.page == self.PAGE_WELCOME else ["!disabled"]
        )
        self.next_button.configure(
            text="Completa configurazione"
            if self.page == self.PAGE_SUMMARY
            else (
                "Verifica e continua"
                if self.page == self.PAGE_CONTROLLER
                and not self._controller_is_current()
                else "Continua"
            )
        )

        renderers = {
            self.PAGE_WELCOME: self._render_welcome,
            self.PAGE_IDENTITY: self._render_identity,
            self.PAGE_CONTROLLER: self._render_controller,
            self.PAGE_RETENTION: self._render_retention,
            self.PAGE_SUMMARY: self._render_summary,
        }
        renderers[self.page]()
        self._fit()

    def _render_welcome(self) -> None:
        self.header_var.set("Benvenuto in Voucher Management")
        self.subtitle_var.set(
            "Questa procedura configura una nuova installazione 5.0. "
            "Le credenziali UniFi vengono usate solo nella sessione corrente "
            "e non vengono salvate."
        )
        ttk.Label(
            self.body,
            text=(
                "La procedura imposta l'identità della postazione e dei voucher, "
                "verifica il controller UniFi e applica la retention conservativa "
                "dello storico locale."
            ),
            wraplength=650,
        ).pack(anchor="w", pady=(18, 8))
        ttk.Label(
            self.body,
            text=(
                "I voucher utilizzati o fisicamente stampati restano protetti. "
                "La retention riguarda solo futuri candidati mai usati e mai "
                "stampati e non esegue cancellazioni automatiche."
            ),
            style="Muted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(8, 0))

    def _render_identity(self) -> None:
        self.header_var.set("Identità della postazione")
        self.subtitle_var.set(
            "Questi dati identificano l'installazione e il layout dei voucher."
        )
        grid = ttk.Frame(self.body)
        grid.pack(fill="x")

        rows = (
            ("Nome installazione", self.installation_name_var),
            ("Descrizione", self.description_var),
            ("Nome struttura", self.structure_name_var),
            ("Titolo Wi-Fi", self.wifi_title_var),
        )
        for row, (label, variable) in enumerate(rows):
            ttk.Label(grid, text=label).grid(
                row=row,
                column=0,
                sticky="w",
                pady=7,
                padx=(0, 18),
            )
            ttk.Entry(
                grid,
                textvariable=variable,
                width=48,
            ).grid(row=row, column=1, sticky="ew", pady=7)

        ttk.Label(grid, text="Profilo struttura").grid(
            row=4,
            column=0,
            sticky="w",
            pady=7,
            padx=(0, 18),
        )
        ttk.Combobox(
            grid,
            textvariable=self.structure_type_var,
            state="readonly",
            values=("Sede", "Evento", "Personalizzata"),
            width=24,
        ).grid(row=4, column=1, sticky="w", pady=7)

        ttk.Label(grid, text="Logo").grid(
            row=5,
            column=0,
            sticky="w",
            pady=7,
            padx=(0, 18),
        )
        logo_row = ttk.Frame(grid)
        logo_row.grid(row=5, column=1, sticky="ew", pady=7)
        ttk.Entry(
            logo_row,
            textvariable=self.logo_var,
            state="readonly",
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            logo_row,
            text="Scegli…",
            command=self._choose_logo,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            logo_row,
            text="Predefinito",
            command=lambda: self.logo_var.set(""),
        ).pack(side="left", padx=(8, 0))
        grid.columnconfigure(1, weight=1)

    def _render_controller(self) -> None:
        self.header_var.set("Controller UniFi")
        self.subtitle_var.set(
            "Verifica il controller con la API ufficiale. La API key viene "
            "cancellata dal campo appena parte il test e rimane solo in memoria."
        )
        grid = ttk.Frame(self.body)
        grid.pack(fill="x")

        ttk.Label(grid, text="Nome profilo").grid(
            row=0, column=0, sticky="w", pady=7, padx=(0, 18)
        )
        ttk.Entry(
            grid,
            textvariable=self.controller_name_var,
        ).grid(row=0, column=1, sticky="ew", pady=7)

        ttk.Label(grid, text="API root / Controller").grid(
            row=1, column=0, sticky="w", pady=7, padx=(0, 18)
        )
        ttk.Entry(
            grid,
            textvariable=self.controller_root_var,
        ).grid(row=1, column=1, sticky="ew", pady=7)

        ttk.Label(grid, text="API key").grid(
            row=2, column=0, sticky="w", pady=7, padx=(0, 18)
        )
        ttk.Entry(
            grid,
            textvariable=self.api_key_var,
            show="•",
        ).grid(row=2, column=1, sticky="ew", pady=7)

        ttk.Label(
            grid,
            textvariable=self.controller_status_var,
            style="Muted.TLabel",
            wraplength=520,
        ).grid(
            row=3,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(14, 0),
        )
        grid.columnconfigure(1, weight=1)

    def _render_retention(self) -> None:
        self.header_var.set("Conservazione dello storico")
        self.subtitle_var.set(
            "I valori raccomandati sono già adatti alla maggior parte delle "
            "installazioni. La pulizia resta sempre sottoposta a revisione."
        )
        ttk.Label(
            self.body,
            text=(
                "Voucher utilizzati: sempre protetti\n"
                "Voucher fisicamente stampati: sempre protetti\n"
                "Mai usati e mai stampati: candidati solo dopo il periodo indicato"
            ),
            wraplength=650,
        ).pack(anchor="w", pady=(18, 16))

        row = ttk.Frame(self.body)
        row.pack(anchor="w")
        ttk.Label(row, text="Età minima candidati").pack(side="left")
        ttk.Spinbox(
            row,
            from_=1,
            to=3650,
            increment=30,
            textvariable=self.retention_days_var,
            width=8,
        ).pack(side="left", padx=(12, 6))
        ttk.Label(row, text="giorni").pack(side="left")

        ttk.Label(
            self.body,
            text=(
                "Nessun voucher viene eliminato automaticamente dal wizard. "
                "La futura pulizia mostrerà sempre i candidati prima di agire."
            ),
            style="Muted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(18, 0))

    def _render_summary(self) -> None:
        self.header_var.set("Riepilogo")
        self.subtitle_var.set(
            "Controllare i dati prima di completare la prima configurazione."
        )
        result = self._controller_result
        info = result.info if result is not None else {}
        rows = (
            ("Installazione", self.installation_name_var.get().strip()),
            ("Struttura", self.structure_name_var.get().strip()),
            ("Titolo Wi-Fi", self.wifi_title_var.get().strip()),
            ("Controller", self.controller_name_var.get().strip()),
            ("API root", self._verified_root or "Non verificato"),
            ("Sito UniFi", str(info.get("siteName") or "—")),
            (
                "Network",
                str(info.get("applicationVersion") or "—"),
            ),
            (
                "Retention",
                f"{self.retention_days_var.get().strip()} giorni",
            ),
            (
                "Logo",
                Path(self.logo_var.get()).name
                if self.logo_var.get().strip()
                else "Predefinito",
            ),
        )
        grid = ttk.Frame(self.body)
        grid.pack(fill="x", pady=(8, 0))
        for row, (label, value) in enumerate(rows):
            ttk.Label(
                grid,
                text=label,
                style="Muted.TLabel",
            ).grid(
                row=row,
                column=0,
                sticky="nw",
                pady=5,
                padx=(0, 18),
            )
            ttk.Label(
                grid,
                text=value or "—",
                wraplength=500,
            ).grid(row=row, column=1, sticky="nw", pady=5)
        grid.columnconfigure(1, weight=1)

    def _choose_logo(self) -> None:
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
            messagebox.showerror(
                "Logo non valido",
                str(exc),
                parent=self,
            )
            return
        self.logo_var.set(selected)

    def _controller_is_current(self) -> bool:
        if self._controller_result is None or not self._verified_root:
            return False
        try:
            return (
                normalize_api_root(self.controller_root_var.get())
                == self._verified_root
            )
        except (ValueError, UniFiApiError):
            return False

    def _validate_identity(self) -> bool:
        required = (
            self.installation_name_var.get().strip(),
            self.structure_name_var.get().strip(),
            self.wifi_title_var.get().strip(),
        )
        if not all(required):
            messagebox.showerror(
                "Prima configurazione",
                "Compilare nome installazione, nome struttura e titolo Wi-Fi.",
                parent=self,
            )
            return False
        return True

    def _validated_retention(self) -> int | None:
        try:
            value = int(self.retention_days_var.get())
            if not 1 <= value <= 3650:
                raise ValueError
            return value
        except (TypeError, ValueError, tk.TclError):
            messagebox.showerror(
                "Prima configurazione",
                "La retention deve essere compresa tra 1 e 3650 giorni.",
                parent=self,
            )
            return None

    def _next(self) -> None:
        if self.page == self.PAGE_IDENTITY and not self._validate_identity():
            return
        if self.page == self.PAGE_CONTROLLER:
            if not self._controller_is_current():
                self._verify_controller()
                return
        if self.page == self.PAGE_RETENTION:
            if self._validated_retention() is None:
                return
        if self.page == self.PAGE_SUMMARY:
            self._finish()
            return
        self.page += 1
        self._render_page()

    def _back(self) -> None:
        if self.page <= self.PAGE_WELCOME:
            return
        self.page -= 1
        self._render_page()

    def _set_probe_busy(self, busy: bool) -> None:
        state = ["disabled"] if busy else ["!disabled"]
        self.next_button.state(state)
        self.back_button.state(state)
        self.cancel_button.state(state)

    def _verify_controller(self) -> None:
        root = self.controller_root_var.get().strip()
        key = self.api_key_var.get()
        self.api_key_var.set("")
        if not key:
            messagebox.showerror(
                "Controller UniFi",
                "Inserire la API key per verificare il controller.",
                parent=self,
            )
            return
        if not self.controller_name_var.get().strip():
            messagebox.showerror(
                "Controller UniFi",
                "Inserire un nome per il profilo controller.",
                parent=self,
            )
            return
        try:
            normalized = normalize_api_root(root)
        except (ValueError, UniFiApiError) as exc:
            messagebox.showerror(
                "Controller UniFi",
                str(exc),
                parent=self,
            )
            return

        settings = self.app.settings_store.load()
        saved_root = str(settings.get("controller_api_root", "")).strip()
        saved_pin = str(
            settings.get("controller_cert_sha256", "")
        ).strip()
        try:
            saved_normalized = (
                normalize_api_root(saved_root) if saved_root else ""
            )
        except (ValueError, UniFiApiError):
            saved_normalized = ""
        trusted_pin = (
            saved_pin
            if saved_pin and saved_normalized == normalized
            else None
        )
        self._start_probe(normalized, key, trusted_pin)

    def _start_probe(
        self,
        api_root: str,
        api_key: str,
        trusted_pin: str | None,
    ) -> None:
        self.controller_status_var.set("Verifica in corso…")

        def worker():
            return probe_controller(
                api_root,
                api_key,
                trusted_cert_sha256=trusted_pin,
            )

        def completed(result: ControllerProbeResult) -> None:
            self._controller_result = result
            self._verified_root = result.client.base_url
            self.controller_root_var.set(result.client.base_url)
            site = result.info.get("siteName") or "sito"
            version = result.info.get("applicationVersion") or "versione sconosciuta"
            tls = (
                "certificato fissato"
                if result.client.trusted_cert_sha256
                else "TLS verificato"
            )
            self.controller_status_var.set(
                f"Verificato • {site} • Network {version} • {tls}"
            )
            self._render_page()

        def failed(exc: Exception) -> None:
            if isinstance(exc, UniFiCertificateChanged):
                previous = _format_fingerprint(exc.previous_fingerprint)
                current = _format_fingerprint(exc.fingerprint)
                accepted = messagebox.askyesno(
                    "Certificato TLS cambiato",
                    "Il certificato del controller è cambiato.\n\n"
                    f"Precedente:\n{previous}\n\n"
                    f"Nuovo:\n{current}\n\n"
                    "La API key non è stata inviata. Verificare la nuova "
                    "impronta prima di autorizzarla.\n\n"
                    "Usare il nuovo certificato?",
                    parent=self,
                )
                if accepted:
                    self._start_probe(api_root, api_key, exc.fingerprint)
                else:
                    self.controller_status_var.set(
                        "Certificato non autorizzato"
                    )
                return

            if isinstance(exc, UniFiCertificateTrustRequired):
                fingerprint = _format_fingerprint(exc.fingerprint)
                accepted = messagebox.askyesno(
                    "Certificato TLS non attendibile",
                    "Il controller usa un certificato non attendibile da "
                    "Windows.\n\n"
                    f"Impronta SHA-256:\n{fingerprint}\n\n"
                    "La API key non è stata inviata. Confermare solo dopo "
                    "aver verificato l'impronta.\n\n"
                    "Autorizzare questo certificato?",
                    parent=self,
                )
                if accepted:
                    self._start_probe(api_root, api_key, exc.fingerprint)
                else:
                    self.controller_status_var.set(
                        "Certificato non autorizzato"
                    )
                return

            self._controller_result = None
            self._verified_root = ""
            self.controller_status_var.set("Verifica non riuscita")
            if isinstance(exc, (UniFiApiError, ValueError)):
                detail = str(exc)
            else:
                self.app.logger.error(
                    "onboarding_controller_probe_failed type=%s",
                    type(exc).__name__,
                )
                detail = "Errore imprevisto durante la verifica del controller."
            messagebox.showerror(
                "Controller UniFi",
                detail,
                parent=self,
            )

        self.app._run_background_task(
            "Verifica controller UniFi…",
            worker,
            completed,
            failed,
            busy_scope=self._set_probe_busy,
        )

    def _managed_logo(self) -> str:
        value = self.logo_var.get().strip()
        if not value:
            return ""
        source = Path(value)
        validate_logo_image(source)
        managed = self.app.paths.persist_configured_logo(value)
        if not managed:
            raise LogoValidationError(
                "Il logo non può essere salvato nella libreria locale"
            )
        managed_path = Path(managed)
        try:
            if managed_path.resolve().parent != self.app.paths.logos.resolve():
                raise LogoValidationError(
                    "Il logo non può essere copiato nella libreria locale"
                )
        except OSError as exc:
            raise LogoValidationError(
                "Il logo non può essere verificato nella libreria locale"
            ) from exc
        return str(managed_path)

    def _finish(self) -> None:
        if not self._validate_identity():
            self.page = self.PAGE_IDENTITY
            self._render_page()
            return
        retention = self._validated_retention()
        if retention is None:
            self.page = self.PAGE_RETENTION
            self._render_page()
            return
        if not self._controller_is_current() or self._controller_result is None:
            self.page = self.PAGE_CONTROLLER
            self._render_page()
            return

        try:
            logo_path = self._managed_logo()
        except (LogoValidationError, OSError) as exc:
            messagebox.showerror(
                "Logo non valido",
                str(exc),
                parent=self,
            )
            self.page = self.PAGE_IDENTITY
            self._render_page()
            return

        observed_at = datetime.now(timezone.utc).isoformat()
        result = self._controller_result
        try:
            # Persist the already verified controller through the same path used
            # by the normal connection UI. The connected client keeps its API
            # key in memory for this application session only.
            self.app._finish_connection(
                result.client,
                result.info,
                result.vouchers,
                profile_name=self.controller_name_var.get().strip(),
                observed_at=result.observed_at,
            )
            draft = OnboardingDraft(
                installation_name=self.installation_name_var.get(),
                description=self.description_var.get(),
                structure_type=self.structure_type_var.get(),
                structure_name=self.structure_name_var.get(),
                wifi_title=self.wifi_title_var.get(),
                logo_path=logo_path,
                pdf_title=self.wifi_title_var.get(),
                pdf_subtitle=self.structure_name_var.get(),
                pdf_contact="",
                pdf_notes="",
                unused_unprinted_days=retention,
            )
            self.app.settings = complete_onboarding(
                self.app.database,
                self.app.settings_store,
                draft,
                observed_at=observed_at,
            )
        except Exception as exc:
            self.app.logger.error(
                "onboarding_complete_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Prima configurazione",
                "La configurazione non è stata completata. I dati verificati "
                "restano in stato sicuro e la procedura potrà essere ripetuta.",
                parent=self,
            )
            return

        messagebox.showinfo(
            "Configurazione completata",
            "Voucher Management è configurato e pronto all'uso.",
            parent=self,
        )
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        if not messagebox.askyesno(
            "Prima configurazione",
            "La configurazione iniziale non è completa.\n\n"
            "Chiudere Voucher Management?",
            parent=self,
        ):
            return
        try:
            self.grab_release()
        except tk.TclError:
            self.app.logger.debug("onboarding_grab_release_ignored")

        self.withdraw()

        def restore_wizard() -> None:
            try:
                if not self.winfo_exists() or not self.app.winfo_exists():
                    return
                self.deiconify()
                self.grab_set()
                self.lift()
            except tk.TclError:
                self.app.logger.debug("onboarding_restore_after_close_cancel_ignored")

        self.app.request_close(on_abort=restore_wizard)
