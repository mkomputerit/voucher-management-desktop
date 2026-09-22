"""Shared application workflow used by the Windows operator UI.

This module owns shared application state plus PDF generation and print/audit
workflow. Voucher creation and the concrete Windows presentation are composed
through focused UI adapters, keeping controller mutation handling isolated from
the stable print path.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from queue import Empty
import sys
import tkinter as tk
from typing import Callable
from tkinter import messagebox

from . import __version__
from .background_tasks import BackgroundResult, start_background_task
from .dialogs import PrintCopiesDialog
from .history import HistoryError, HistoryService
from .identity import PRODUCT_NAME
from .logging_utils import configure_logging
from .mutation_guard import CreateMutationGuard
from .paths import AppPaths
from .pdf_fonts import UnsupportedPdfTextError
from .pdf_preview import PdfPreview
from .pdf_render import render_batch_pdf
from .print_archive import (
    DEFAULT_PRINT_RETENTION_DAYS,
    cleanup_orphan_pdf_temps,
    cleanup_print_archive,
)
from .settings import SettingsStore
from .voucher_creation_ui import VoucherCreationMixin
from .security.history_key import HistoryKeyStore
from .unifi_api import ApiVoucher, UniFiApiError
from .workflows import (
    ExistingPdfResolutionError,
    execute_print_job,
    prepare_print_job,
    refresh_vouchers,
    resolve_existing_pdf,
    verify_print_history_ready,
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


class VoucherApp(VoucherCreationMixin, tk.Tk):
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
        self.create_guard = CreateMutationGuard(self.paths.pending_create)
        self.settings_store = SettingsStore(self.paths.settings)
        self.settings = self.settings_store.load()
        settings_warning = self.settings_store.consume_warning()

        migrated_logo = self.paths.persist_configured_logo(
            self.settings.get("logo_path", "")
        )
        logo_warning = self.paths.consume_logo_warning()
        if migrated_logo != self.settings.get("logo_path", ""):
            self.settings["logo_path"] = migrated_logo
            self.settings_store.save(self.settings)

        self.logger = configure_logging(
            self.paths.logs,
            int(self.settings.get("log_retention_days", 30)),
        )
        self._cleanup_orphan_pdf_temps()
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

        pending_print_recovery_error = None
        try:
            if self.history.recover_pending_print_audit():
                self.logger.info("pending_print_audit_recovered")
        except HistoryError as exc:
            pending_print_recovery_error = str(exc)
            self.logger.warning(
                "pending_print_audit_recovery_failed type=%s",
                type(exc).__name__,
            )

        self._cleanup_print_archive()
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
        self._background_results = None
        self._background_success = None
        self._background_error = None
        self._background_scope = None
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
        if logo_warning:
            messagebox.showwarning(
                "Logo rimosso",
                logo_warning,
                parent=self,
            )
        if pending_print_recovery_error:
            messagebox.showwarning(
                "Registrazione stampa pendente",
                "Una stampa precedente risulta ancora da registrare nello "
                "storico. Nuove stampe restano bloccate finché il recupero "
                "non viene completato.\n\n"
                f"{pending_print_recovery_error}",
                parent=self,
            )
        if self.create_guard.pending:
            messagebox.showwarning(
                "Creazione da verificare",
                "Una precedente creazione potrebbe essere stata inviata al "
                "controller senza ricevere una risposta definitiva. Nuove "
                "creazioni restano bloccate. Eseguire Aggiorna e verificare "
                "l'elenco prima di creare altri voucher.",
                parent=self,
            )

    def _cleanup_orphan_pdf_temps(self) -> None:
        """Remove stale renderer temp files left behind by a hard crash."""

        try:
            removed = cleanup_orphan_pdf_temps(self.paths.prints)
        except Exception as exc:
            self.logger.warning(
                "pdf_temp_cleanup_skipped type=%s",
                type(exc).__name__,
            )
            return

        if removed:
            self.logger.info(
                "pdf_temp_cleanup removed=%d",
                len(removed),
            )

    def _cleanup_print_archive(self) -> None:
        """Apply PDF retention without touching files outside the audit trail."""

        try:
            retention_days = int(
                self.settings.get(
                    "print_retention_days",
                    DEFAULT_PRINT_RETENTION_DAYS,
                )
            )
            managed_names = self.history.generated_output_names()
            removed = cleanup_print_archive(
                self.paths.prints,
                retention_days,
                managed_names,
            )
        except Exception as exc:
            self.logger.warning(
                "print_archive_cleanup_skipped type=%s",
                type(exc).__name__,
            )
            return

        if removed:
            self.logger.info(
                "print_archive_cleanup removed=%d retention_days=%d",
                len(removed),
                retention_days,
            )

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

    def _set_background_busy(self, busy: bool, label: str = "") -> None:
        """Let the concrete UI expose one application-wide blocking operation."""

    def _set_network_busy(self, busy: bool, label: str = "") -> None:
        """Compatibility wrapper for older network-specific call sites."""

        self._set_background_busy(busy, label)

    def _show_network_error(
        self,
        title: str,
        exc: Exception,
        *,
        prefix: str = "",
    ) -> None:
        """Show expected API errors verbatim and redact unexpected failures."""

        if isinstance(exc, (UniFiApiError, ValueError)):
            detail = str(exc)
        else:
            self.logger.error(
                "network_task_failed type=%s",
                type(exc).__name__,
            )
            detail = (
                "Errore imprevisto durante la comunicazione con il controller."
            )
        messagebox.showerror(
            title,
            f"{prefix}{detail}",
            parent=self,
        )

    def _run_background_task(
        self,
        label: str,
        worker: Callable[[], object],
        on_success: Callable[[object], None],
        on_error: Callable[[Exception], None],
        *,
        busy_scope: Callable[[bool], None] | None = None,
    ) -> bool:
        """Run one blocking operation off Tk and marshal completion to Tk.

        The application intentionally serializes background work. This avoids
        overlapping controller changes, history mutations, backup/restore,
        rendering and printer submission while still keeping Tk responsive.
        Workers must not access Tk widgets.
        """

        if self._background_results is not None:
            self.bell()
            return False

        self._background_success = on_success
        self._background_error = on_error
        self._background_scope = busy_scope
        self._background_results = start_background_task(worker)
        self._set_background_busy(True, label)
        if busy_scope is not None:
            busy_scope(True)
        self.after(40, self._poll_background_task)
        return True

    def _poll_background_task(self) -> None:
        """Consume one worker result from the Tk thread."""

        results = self._background_results
        if results is None:
            return
        try:
            outcome: BackgroundResult = results.get_nowait()
        except Empty:
            self.after(40, self._poll_background_task)
            return

        on_success = self._background_success
        on_error = self._background_error
        busy_scope = self._background_scope
        self._background_results = None
        self._background_success = None
        self._background_error = None
        self._background_scope = None

        try:
            ui_alive = bool(self.winfo_exists())
        except tk.TclError:
            ui_alive = False
        if not ui_alive:
            return

        self._set_background_busy(False)
        if busy_scope is not None:
            busy_scope(False)

        if outcome.error is not None:
            if on_error is not None:
                on_error(outcome.error)
            return
        if on_success is not None:
            on_success(outcome.value)

    def _run_network_task(
        self,
        label: str,
        worker: Callable[[], object],
        on_success: Callable[[object], None],
        on_error: Callable[[Exception], None],
    ) -> bool:
        """Compatibility wrapper for controller-specific call sites."""

        return self._run_background_task(
            label,
            worker,
            on_success,
            on_error,
        )

    def _poll_network_task(self) -> None:
        """Compatibility wrapper for tests/callers from the network-only era."""

        self._poll_background_task()

    def refresh(self):
        if not self.client:
            messagebox.showinfo(
                "UniFi",
                "Connettersi prima al controller UniFi",
                parent=self,
            )
            return

        client = self.client

        def completed(vouchers) -> None:
            self.vouchers = list(vouchers)
            try:
                self.create_guard.clear()
            except CreateMutationGuardError as exc:
                self.logger.warning(
                    "create_guard_clear_failed type=%s",
                    type(exc).__name__,
                )
                messagebox.showwarning(
                    "Creazione ancora sospesa",
                    "L'elenco è stato aggiornato, ma non è stato possibile "
                    "rimuovere il blocco anti-ripetizione. La creazione resta "
                    "sospesa per sicurezza.",
                    parent=self,
                )
            self.populate()

        def failed(exc: Exception) -> None:
            self._show_network_error(
                "Sincronizzazione",
                exc,
            )

        self._run_network_task(
            "Aggiornamento voucher…",
            lambda: refresh_vouchers(client),
            completed,
            failed,
        )

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

    def print_selected(self):
        """Collect operator input, then delegate print preparation/execution."""
        selected = self.selected()
        if not selected:
            messagebox.showinfo(
                "Stampa",
                "Selezionare uno o più voucher attivi dalla prima colonna",
                parent=self,
            )
            return

        try:
            verify_print_history_ready(
                selected,
                history=self.history,
                settings=self.settings,
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

        job = prepare_print_job(
            selected,
            self.paths.prints,
            unlimited_copies=copies,
            now=datetime.now(),
        )
        settings = dict(self.settings)
        history = self.history

        def worker():
            return execute_print_job(
                job,
                history=history,
                settings=settings,
                render_pdf=render_batch_pdf,
            )

        def completed(outcome) -> None:
            self.last_pdf = outcome.output
            self.populate()
            self._preview(
                outcome.output,
                list(outcome.codes),
            )

        def failed(exc: Exception) -> None:
            if isinstance(exc, UnsupportedPdfTextError):
                self.logger.warning(
                    "pdf_generation_blocked unsupported_glyphs"
                )
                messagebox.showwarning(
                    "Caratteri non supportati",
                    str(exc),
                    parent=self,
                )
                return
            if isinstance(exc, HistoryError):
                messagebox.showerror(
                    "Cronologia non disponibile",
                    str(exc),
                    parent=self,
                )
                return
            self.logger.error(
                "pdf_generation_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Stampa",
                "Impossibile generare il PDF selezionato.",
                parent=self,
            )

        self._run_background_task(
            "Generazione PDF…",
            worker,
            completed,
            failed,
        )

    def _preview(self, path: Path, codes: list[str]):
        PdfPreview(self, path, codes, self.history, self.settings, on_print=self.populate)

    def open_existing_pdf(self):
        selected = self.selected()
        if len(selected) != 1:
            messagebox.showinfo(
                "Apri PDF",
                "Selezionare un solo voucher attivo.",
                parent=self,
            )
            return

        voucher = selected[0]
        try:
            resolved = resolve_existing_pdf(
                voucher,
                self.vouchers,
                history=self.history,
                settings=self.settings,
                prints_root=self.paths.prints,
            )
        except HistoryError as exc:
            messagebox.showerror(
                "Cronologia non disponibile",
                str(exc),
                parent=self,
            )
            return
        except ExistingPdfResolutionError as exc:
            if exc.reason == "not_recorded":
                messagebox.showinfo(
                    "Apri PDF",
                    "Per questo voucher non risulta alcun PDF archiviato.",
                    parent=self,
                )
            elif exc.reason == "missing_file":
                messagebox.showerror(
                    "Apri PDF",
                    "Il file registrato nello storico non è più disponibile. "
                    "Potrebbe essere stato eliminato dalla retention PDF "
                    "configurata oppure spostato manualmente.\n\n"
                    f"{exc.path}",
                    parent=self,
                )
            elif exc.reason == "linkage_mismatch":
                messagebox.showerror(
                    "Apri PDF",
                    "Il collegamento tra voucher e PDF non è verificabile nello "
                    "storico locale.",
                    parent=self,
                )
            else:
                messagebox.showerror(
                    "Apri PDF",
                    "Impossibile risolvere il PDF archiviato.",
                    parent=self,
                )
            return

        try:
            self._preview(
                resolved.path,
                list(resolved.linked_codes),
            )
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
