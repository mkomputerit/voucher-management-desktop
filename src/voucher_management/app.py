"""Shared application workflow used by the Windows operator UI.

This module owns shared application state plus PDF generation and print/audit
workflow. Voucher creation and the concrete Windows presentation are composed
through focused UI adapters, keeping controller mutation handling isolated from
the stable print path.
"""

from __future__ import annotations

from datetime import datetime, timezone
import getpass
import logging
import os
from pathlib import Path
from queue import Empty
import sys
import tkinter as tk
from uuid import uuid4
from typing import Callable
from tkinter import messagebox

from . import __version__
from .background_tasks import BackgroundResult, start_background_task
from .create_reporting_recovery import (
    reconcile_pending_create_reporting,
    reconcile_pending_create_reporting_to_path,
)
from .database import Database
from .dialogs import PrintCopiesDialog, ReprintConfirmDialog
from .history import HistoryError, HistoryService
from .identity import PRODUCT_NAME
from .logging_utils import configure_logging
from .mutation_guard import CreateMutationGuard, CreateMutationGuardError
from .paths import AppPaths
from .pdf_fonts import UnsupportedPdfTextError
from .pdf_preview import PdfPreview
from .pdf_render import render_batch_pdf
from .reprint_policy import evaluate_reprint
from .print_archive import (
    DEFAULT_PRINT_RETENTION_DAYS,
    cleanup_orphan_pdf_temps,
    cleanup_print_archive,
)
from .settings import SettingsStore
from .single_instance import InstanceAlreadyRunning, SingleInstanceGuard
from .sync_store import (
    inspect_snapshot_absences_to_path,
    load_local_vouchers,
    persist_connection_snapshot_to_path,
    persist_refresh_snapshot_to_path,
)
from .voucher_creation_ui import VoucherCreationMixin
from .security.history_key import HistoryKeyStore
from .security_revocation import reconcile_pending_security_revocations_to_path
from .unifi_api import ApiVoucher, UniFiApiError
from .workflows import (
    ExistingPdfResolutionError,
    execute_print_job,
    prepare_print_job,
    refresh_vouchers,
    resolve_existing_pdf,
    verify_print_history_ready,
    verify_snapshot_absences,
)


LOGGER = logging.getLogger("voucher_management.app")


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


def print_action_label(count: int) -> str:
    """Return the operator-facing print action without workflow jargon."""

    count = max(0, int(count))
    return (
        f"Stampa selezionati ({count})"
        if count
        else "Stampa selezionati"
    )


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
                LOGGER.debug("runtime_iconbitmap_unavailable")
        self.title(f"{PRODUCT_NAME} {__version__}")
        self.minsize(1120, 650)
        self.geometry("1420x780")
        try:
            self.paths = AppPaths()
        except Exception as exc:
            messagebox.showerror(
                "Avvio impossibile",
                "Configurazione dell'installazione non valida.\n\n"
                f"{exc}",
                parent=self,
            )
            self.destroy()
            return
        self.instance_guard = SingleInstanceGuard(self.paths.instance_lock)
        try:
            self.instance_guard.acquire()
        except InstanceAlreadyRunning as exc:
            messagebox.showwarning("Voucher Management già aperto", str(exc), parent=self)
            self.destroy()
            return
        try:
            self.paths.ensure_writable()
        except Exception as exc:
            # The <Destroy> release hook is installed only after this check.
            # Release explicitly so a recoverable startup error never keeps
            # this Windows user locked out for the lifetime of the process.
            self.instance_guard.release()
            messagebox.showerror("Avvio impossibile", f"Cartella dell'applicazione non scrivibile.\n\n{exc}")
            self.destroy()
            return
        # Keep ownership until the root window is destroyed.
        self.bind("<Destroy>", self._release_instance_guard, add="+")
        self.database = Database(self.paths.database)
        try:
            self.database.initialize()
            self.database.integrity_check()
            self.session_uuid = uuid4().hex
            self.database.start_application_session(
                session_uuid=self.session_uuid,
                windows_user=self._windows_operator_identity(),
                started_at=datetime.now(timezone.utc).isoformat(),
                app_version=__version__,
            )
        except Exception:
            self.database.close()
            self.instance_guard.release()
            raise
        self.active_controller_id = None
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
        try:
            if reconcile_pending_create_reporting(
                self.database,
                self.paths.pending_create_reporting,
            ):
                self.logger.info("create_reporting_reconciled_on_startup")
        except Exception as exc:
            self.logger.error(
                "create_reporting_startup_reconcile_failed type=%s",
                type(exc).__name__,
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

        # Recovery is delayed until the SQLite controller snapshot is loaded.
        # A submitted print marker must not be cleared after repairing only the
        # HMAC history, otherwise a crash could permanently omit the 5.0 audit.
        pending_print_recovery_error = None

        self._cleanup_print_archive()
        if settings_warning:
            messagebox.showwarning(
                "Impostazioni ripristinate",
                settings_warning,
                parent=self,
            )
        self.client = None
        # Home operational metrics are intentionally blank until this process
        # has completed a real controller list operation. The local SQLite
        # snapshot remains available to history/reporting but is not presented
        # as current controller state.
        self.controller_snapshot_live = False
        # Milestone A can reopen the last durable snapshot before any network
        # request. The timestamp/status remains explicitly local until connect.
        saved_api_root = str(self.settings.get("controller_api_root", "")).strip()
        saved_site_id = str(self.settings.get("controller_site_id", "")).strip()
        if saved_api_root:
            self.active_controller_id = self.database.find_controller_by_identity(
                api_root=saved_api_root,
                site_id=saved_site_id,
            )
        self.vouchers = (
            load_local_vouchers(
                self.database,
                controller_id=self.active_controller_id,
            )
            if self.active_controller_id is not None
            else []
        )

        try:
            pending_state = self.history.pending_print_state()
            if pending_state == "submitted":
                if self.history.recover_pending_print_audit(
                    clear_pending=False
                ):
                    self._record_pending_print_sqlite_and_finalize()
                    self.logger.info("pending_print_audit_recovered")
            elif pending_state == "prepared":
                # Preserve the existing fail-closed behavior: only an operator
                # may decide whether an interrupted Windows submission printed.
                self.history.recover_pending_print_audit()
        except HistoryError as exc:
            pending_print_recovery_error = str(exc)
            self.logger.warning(
                "pending_print_audit_recovery_failed type=%s",
                type(exc).__name__,
            )
        except Exception as exc:
            pending_print_recovery_error = (
                "Lo storico della stampa è disponibile, ma l'archivio SQLite "
                "non è stato completato. Usare Recupera stampa pendente."
            )
            self.logger.warning(
                "pending_sqlite_print_recovery_failed type=%s",
                type(exc).__name__,
            )

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
        self.connection_var = tk.StringVar(
            value=(
                "Modalità locale"
                if self.active_controller_id is not None
                else "Non connesso"
            )
        )
        # Default to the operator's real task rather than the complete archive.
        self.filter_var = tk.StringVar(value="Da stampare")
        self.search_var = tk.StringVar()
        self.count_var = tk.StringVar(value="0 voucher")
        self.action_var = tk.StringVar(value=print_action_label(0))
        self._build_ui()
        # Route only an ordinary window-manager close through the 5.0
        # disaster-recovery workflow. Internal destroy() calls used after a
        # successful restore/migration intentionally bypass this hook.
        close_handler = getattr(self, "request_close", self.destroy)
        self.protocol("WM_DELETE_WINDOW", close_handler)
        self._populate_initial_snapshot()
        retention_intro = getattr(
            self,
            "show_retention_intro_if_needed",
            None,
        )
        retention_intro_allowed = getattr(
            self,
            "_retention_intro_allowed_on_startup",
            None,
        )
        if retention_intro is not None and (
            retention_intro_allowed is None or retention_intro_allowed()
        ):
            # Avoid racing the first Windows mapping with a transient/grabbed
            # retention dialog in a console-less packaged build.
            self.after(380, retention_intro)
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

    def _populate_initial_snapshot(self) -> None:
        """Render the already-loaded SQLite snapshot on first UI display.

        The local snapshot is loaded before widgets are created. Populate only
        after _build_ui() so offline startup immediately shows durable vouchers
        instead of waiting for an operator refresh/filter action.
        """

        self.populate()

    def _release_instance_guard(self, event) -> None:
        """Release ownership only when the root Tk window is destroyed."""
        if event.widget is self:
            database = getattr(self, "database", None)
            if database is not None:
                try:
                    database.close()
                finally:
                    self.database = None
            self.instance_guard.release()

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
        except Exception as dialog_exc:
            # Tk may itself be tearing down; logging above remains available.
            LOGGER.debug(
                "unhandled_error_dialog_failed type=%s",
                type(dialog_exc).__name__,
            )

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

    def _finalize_voucher_operation_ui(
        self,
        *,
        operation: str,
        refresh_reports: bool = True,
    ) -> bool:
        """Apply the global post-operation UI invariant after a committed change.

        Successful voucher operations always clear the shared Home/Voucher
        selection and rebuild every local projection from durable facts plus the
        current in-memory controller snapshot.  This is deliberately not a
        controller synchronization.
        """

        self.checked_ids.clear()
        try:
            self.populate()
            refresh_thresholds = getattr(
                self,
                "_refresh_threshold_summary",
                None,
            )
            if callable(refresh_thresholds):
                refresh_thresholds()
            if refresh_reports:
                refresh_report = getattr(
                    self,
                    "_refresh_report_summary",
                    None,
                )
                if callable(refresh_report):
                    refresh_report()
            return True
        except Exception as exc:
            self.logger.warning(
                "voucher_operation_ui_refresh_failed operation=%s type=%s",
                str(operation or "unknown"),
                type(exc).__name__,
            )
            # Even when the full rebuild fails, never leave the completed
            # operation's previous blue selection armed in Home/Voucher.
            try:
                self._sync_selection_ui()
            except Exception as selection_exc:
                self.logger.warning(
                    "voucher_operation_selection_clear_failed type=%s",
                    type(selection_exc).__name__,
                )
            messagebox.showwarning(
                "Operazione completata • interfaccia da aggiornare",
                "L'operazione è stata completata e i dati sono stati salvati, "
                "ma l'aggiornamento automatico dell'interfaccia non è riuscito "
                "completamente. La selezione è stata azzerata. Riaprire la vista "
                "interessata; usare Sincronizza solo se serve rileggere nuovi "
                "dati dalla controller UniFi.",
                parent=self,
            )
            return False

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
        controller_id = getattr(self, "active_controller_id", None)
        database_path = Path(self.paths.database)
        cached_snapshot = list(self.vouchers)
        operator = VoucherApp._windows_operator_identity()

        def worker():
            listed_snapshot = list(refresh_vouchers(client))
            persist_snapshot = list(listed_snapshot)
            display_snapshot = list(listed_snapshot)
            confirmed_absent_ids: frozenset[str] = frozenset()
            snapshot_authoritative = True
            archive_error = None
            resolved_controller_id = controller_id
            observed_at = datetime.now(timezone.utc).isoformat()
            try:
                if controller_id is not None:
                    present_ids = frozenset(
                        str(voucher.id)
                        for voucher in listed_snapshot
                        if str(getattr(voucher, "id", "") or "").strip()
                    )
                    plan = inspect_snapshot_absences_to_path(
                        database_path,
                        controller_id=controller_id,
                        present_unifi_ids=present_ids,
                    )
                    verification = verify_snapshot_absences(
                        client,
                        listed_snapshot,
                        plan.confirmation_ids,
                    )
                    persist_snapshot = list(verification.vouchers)
                    confirmed_absent_ids = verification.confirmed_absent_ids
                    unresolved_ids = (
                        set(plan.suspected_ids)
                        - set(verification.recovered_ids)
                        - set(verification.confirmed_absent_ids)
                    )
                    snapshot_authoritative = not unresolved_ids

                    display_by_id = {
                        str(voucher.id): voucher
                        for voucher in persist_snapshot
                    }
                    if unresolved_ids:
                        cached_by_id = {
                            str(voucher.id): voucher
                            for voucher in cached_snapshot
                        }
                        for remote_id in unresolved_ids:
                            cached = cached_by_id.get(remote_id)
                            if cached is not None:
                                display_by_id[remote_id] = cached
                    display_snapshot = list(display_by_id.values())

                    persist_refresh_snapshot_to_path(
                        database_path,
                        controller_id=controller_id,
                        vouchers=persist_snapshot,
                        observed_at=observed_at,
                        confirmed_absent_ids=confirmed_absent_ids,
                    )
                else:
                    # A live controller can exist even when the first local
                    # persistence attempt failed. A later Sync must be able to
                    # create the missing durable controller profile.
                    base_url = str(getattr(client, "base_url", "") or "").strip()
                    if base_url and database_path is not None:
                        name_var = getattr(self, "controller_name_var", None)
                        requested_name = (
                            str(name_var.get()).strip()
                            if name_var is not None
                            else ""
                        )
                        persisted = persist_connection_snapshot_to_path(
                            database_path,
                            api_root=base_url,
                            cert_sha256=str(
                                getattr(client, "trusted_cert_sha256", "") or ""
                            ),
                            requested_name=requested_name,
                            site_name=str(
                                getattr(client, "site_name", "") or requested_name
                            ),
                            site_id=str(getattr(client, "site_id", "") or ""),
                            vouchers=listed_snapshot,
                            observed_at=observed_at,
                        )
                        resolved_controller_id = persisted.controller_id
                        if persisted.suspected_absence_ids:
                            snapshot_authoritative = False
                            cached_by_id = {
                                str(voucher.id): voucher
                                for voucher in cached_snapshot
                            }
                            display_by_id = {
                                str(voucher.id): voucher
                                for voucher in listed_snapshot
                            }
                            for remote_id in persisted.suspected_absence_ids:
                                cached = cached_by_id.get(remote_id)
                                if cached is not None:
                                    display_by_id[remote_id] = cached
                            display_snapshot = list(display_by_id.values())

                if resolved_controller_id is not None:
                    marker_path = getattr(
                        self.paths,
                        "pending_create_reporting",
                        Path(database_path).with_name(
                            "pending_create_reporting.json"
                        ),
                    )
                    reconcile_pending_create_reporting_to_path(
                        database_path,
                        marker_path,
                        controller_id=resolved_controller_id,
                    )
                    reconcile_pending_security_revocations_to_path(
                        database_path,
                        controller_id=resolved_controller_id,
                        live_voucher_ids=frozenset(
                            str(voucher.id)
                            for voucher in persist_snapshot
                            if str(getattr(voucher, "id", "") or "").strip()
                        ),
                        confirmed_absent_ids=confirmed_absent_ids,
                        observed_at=observed_at,
                        windows_user=operator,
                    )
            except Exception as exc:
                archive_error = exc
            return (
                display_snapshot,
                archive_error,
                resolved_controller_id,
                snapshot_authoritative,
            )

        def completed(result) -> None:
            if (
                isinstance(result, tuple)
                and len(result) == 4
                and (
                    result[1] is None
                    or isinstance(result[1], Exception)
                )
            ):
                (
                    vouchers,
                    archive_error,
                    resolved_controller_id,
                    snapshot_authoritative,
                ) = result
            elif (
                isinstance(result, tuple)
                and len(result) == 3
                and (
                    result[1] is None
                    or isinstance(result[1], Exception)
                )
            ):
                vouchers, archive_error, resolved_controller_id = result
                snapshot_authoritative = True
            elif (
                isinstance(result, tuple)
                and len(result) == 2
                and (
                    result[1] is None
                    or isinstance(result[1], Exception)
                )
            ):
                vouchers, archive_error = result
                resolved_controller_id = controller_id
                snapshot_authoritative = True
            else:
                # Compatibility with thin adapters/tests that invoke the
                # success callback directly with a voucher sequence.
                vouchers, archive_error = result, None
                resolved_controller_id = controller_id
                snapshot_authoritative = True

            if resolved_controller_id is not None:
                self.active_controller_id = resolved_controller_id
            snapshot = list(vouchers)
            self.vouchers = snapshot
            self.controller_snapshot_live = bool(snapshot_authoritative)
            if archive_error is None and snapshot_authoritative:
                callback = getattr(self, "_controller_operation_succeeded", None)
                if callback is not None:
                    callback()
            elif archive_error is None:
                callback = getattr(self, "_controller_operation_stale", None)
                if callback is not None:
                    callback()
            else:
                callback = getattr(self, "_controller_operation_stale", None)
                if callback is not None:
                    callback(archive_failed=True)
                logger = getattr(self, "logger", LOGGER)
                logger.error(
                    "refresh_archive_persistence_failed type=%s",
                    type(archive_error).__name__,
                )
                messagebox.showwarning(
                    "Controller aggiornato • archivio locale da verificare",
                    "La controller ha restituito l'elenco aggiornato, ma non è "
                    "stato possibile salvarlo completamente nello storico locale. "
                    + (
                        "I numeri Home sono live; "
                        if snapshot_authoritative
                        else "I numeri Home restano da verificare; "
                    )
                    + "i Report potrebbero essere incompleti finché un "
                    "aggiornamento non riesce.",
                    parent=self,
                )
            self.populate()

            recovery_owns_guard = False
            if snapshot_authoritative:
                recovery = getattr(
                    self,
                    "_offer_uncertain_create_recovery_after_refresh",
                    None,
                )
                if callable(recovery):
                    recovery_owns_guard = bool(recovery(snapshot))
            try:
                if snapshot_authoritative and not recovery_owns_guard:
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

        def failed(exc: Exception) -> None:
            retry = getattr(
                self,
                "_handle_controller_refresh_failure",
                None,
            )
            if callable(retry) and bool(retry(exc)):
                return
            callback = getattr(self, "_controller_operation_failed", None)
            if callback is not None:
                callback()
            self._show_network_error(
                "Sincronizzazione",
                exc,
            )

        self._run_network_task(
            "Aggiornamento voucher…",
            worker,
            completed,
            failed,
        )

    def populate(self) -> None:
        """Render vouchers in the concrete operator UI."""
        raise NotImplementedError

    def _sync_selection_ui(self, iids=None) -> None:
        """Update checkbox marks without rebuilding the voucher table.

        Selection changes are purely local UI state. Re-running populate()
        here would reread history, destroy every Treeview row and insert the
        complete visible set again, which causes a noticeable refresh on each
        click. Keep the expensive full render for real data/filter changes.
        """

        target_iids = tuple(self.by_iid) if iids is None else tuple(iids)
        for iid in target_iids:
            voucher = self.by_iid.get(iid)
            if voucher is None:
                continue

            values = list(self.tree.item(iid, "values"))
            if not values:
                continue

            if self._is_expired(voucher):
                mark = "—"
            else:
                mark = "☑" if voucher.id in self.checked_ids else "☐"

            if values[0] != mark:
                values[0] = mark
                self.tree.item(iid, values=values)

        self.count_var.set(
            f"{len(self.by_iid)} visualizzati  •  "
            f"{len(self.checked_ids)} selezionati"
        )
        self.action_var.set(print_action_label(len(self.checked_ids)))

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
        self._sync_selection_ui((iid,))
        return "break"

    def toggle_all_visible(self):
        # Expired rows may be visible in archive views but are never selectable.
        visible = {v.id for v in self.by_iid.values() if not self._is_expired(v)}
        if visible and visible.issubset(self.checked_ids):
            self.checked_ids.difference_update(visible)
        else:
            self.checked_ids.update(visible)
        self._sync_selection_ui()

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
                "Selezionare uno o più voucher attivi nella tabella",
                parent=self,
            )
            return

        alignment_ready = getattr(self, "_voucher_alignment_ready", None)
        if callable(alignment_ready):
            unaligned = [
                voucher
                for voucher in selected
                if not alignment_ready(voucher)
            ]
            if unaligned:
                messagebox.showinfo(
                    "Allineamento richiesto",
                    (
                        "Uno o più voucher selezionati devono ancora essere "
                        "allineati prima della stampa.\n\n"
                        "Usare “Allinea…” per completare nominalità e stato "
                        "di stampa locale, quindi riprovare."
                    ),
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

        site_id = (
            self.database.controller_site_id(self.active_controller_id)
            if self.active_controller_id is not None
            else ""
        )
        job = prepare_print_job(
            selected,
            self.paths.prints,
            unlimited_copies=copies,
            now=datetime.now(),
            site_id=site_id,
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
            self._finalize_voucher_operation_ui(
                operation="pdf_generation",
            )
            self._preview(
                outcome.output,
                list(outcome.codes),
                site_id=job.batch.site_id,
                unifi_ids=[
                    item.unifi_id
                    for item in job.batch.vouchers
                ],
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

    @staticmethod
    def _windows_operator_identity() -> str:
        """Return a stable local audit label without persisting credentials."""

        username = str(os.environ.get("USERNAME") or getpass.getuser()).strip()
        domain = str(os.environ.get("USERDOMAIN") or "").strip()
        if domain and username:
            return f"{domain}\\{username}"
        return username or "unknown"

    def _confirm_physical_reprint(
        self,
        codes: list[str],
        parent,
        *,
        unifi_ids: list[str] | None = None,
    ) -> bool:
        """Confirm physical duplicates using durable SQLite print facts."""

        if self.active_controller_id is None:
            messagebox.showerror(
                "Stampa",
                "Impossibile verificare lo storico delle ristampe senza "
                "un controller locale associato.",
                parent=parent,
            )
            return False

        try:
            stable_ids = (
                [str(value).strip() for value in unifi_ids]
                if unifi_ids is not None
                else []
            )
            if stable_ids and len(stable_ids) == len(codes):
                summaries = self.database.print_summaries_for_remote_ids(
                    controller_id=self.active_controller_id,
                    unifi_ids=stable_ids,
                )
                summary_mode = "uuid"
            else:
                summaries = self.database.print_summaries_for_codes(
                    controller_id=self.active_controller_id,
                    codes=list(codes),
                )
                summary_mode = "code"
        except Exception as exc:
            self.logger.warning(
                "reprint_preflight_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Stampa",
                "Impossibile verificare in modo sicuro se i voucher siano "
                "già stati stampati. La stampa viene sospesa.",
                parent=parent,
            )
            return False

        usage_states = {}
        if summary_mode == "uuid":
            try:
                usage_states = self.database.usage_state_for_remote_ids(
                    controller_id=self.active_controller_id,
                    unifi_ids=stable_ids,
                )
            except Exception as exc:
                self.logger.warning(
                    "reprint_usage_preflight_failed type=%s",
                    type(exc).__name__,
                )
                messagebox.showerror(
                    "Stampa",
                    "Impossibile verificare in modo sicuro l'utilizzo storico "
                    "dei voucher. La stampa viene sospesa.",
                    parent=parent,
                )
                return False

        unknown_print_count = sum(
            1
            for summary in summaries.values()
            if (
                summary.print_jobs <= 0
                and str(
                    getattr(summary, "print_state", "UNKNOWN") or "UNKNOWN"
                ).strip().upper() == "UNKNOWN"
            )
        )
        if unknown_print_count:
            subject = (
                "questo voucher"
                if unknown_print_count == 1
                else f"questi {unknown_print_count} voucher"
            )
            if not messagebox.askyesno(
                "Stampa non determinabile",
                (
                    f"Per {subject} lo stato di stampa precedente non è "
                    "determinabile. Potrebbe essere già stato stampato o "
                    "consegnato fuori da Voucher Management.\n\n"
                    "Vuoi procedere comunque con la stampa?\n\n"
                    "Se la stampa verrà effettivamente inviata, da quel "
                    "momento sarà registrata come stampa verificata."
                ),
                parent=parent,
            ):
                return False

        warnings = []
        seen: set[str] = set()
        for index, display_code in enumerate(codes):
            canonical = str(display_code).strip().replace("-", "")
            identity = (
                stable_ids[index]
                if summary_mode == "uuid"
                else canonical
            )
            if not identity or identity in seen:
                continue
            seen.add(identity)
            warning = evaluate_reprint(
                summaries[identity],
                ever_used=(
                    usage_states.get(identity)
                    if summary_mode == "uuid"
                    else None
                ),
            )
            if warning.required:
                warnings.append((str(display_code), warning))

        if not warnings:
            return True
        return bool(ReprintConfirmDialog(parent, warnings).result)

    def _record_sqlite_print_audit(
        self,
        pending: dict,
        codes: list[str],
        pdf_path: Path,
        *,
        unifi_ids: list[str] | None = None,
    ) -> None:
        """Mirror a confirmed physical print into the 5.0 SQLite audit."""

        if self.active_controller_id is None:
            raise RuntimeError(
                "Controller locale non associato alla stampa"
            )
        self.database.record_print_audit(
            controller_id=self.active_controller_id,
            audit_id=str(pending["audit_id"]),
            codes=list(codes),
            output_file=Path(pdf_path).name,
            unifi_ids=(
                list(unifi_ids)
                if unifi_ids is not None
                else None
            ),
            document_copies=int(pending["copies"]),
            printed_at=str(pending["submitted_at"]),
            windows_user=self._windows_operator_identity(),
        )

    def _record_pending_print_sqlite_and_finalize(self) -> bool:
        """Complete SQLite audit for a submitted crash-recovery marker.

        Milestone A data is per Windows user, so reopening the same LocalAppData
        root also identifies the same operator scope. The HMAC descriptor is
        resolved only against voucher codes already present in SQLite.
        """

        site_id = (
            self.database.controller_site_id(self.active_controller_id)
            if self.active_controller_id is not None
            else ""
        )
        details = self.history.resolve_pending_print(
            [voucher.code_formatted for voucher in self.vouchers],
            self.settings,
            site_id=site_id,
            candidate_unifi_ids=[
                str(voucher.id)
                for voucher in self.vouchers
            ],
        )
        if details is None:
            return False
        if details.state != "submitted":
            raise HistoryError(
                "La stampa pendente non è ancora confermata come inviata"
            )

        self._record_sqlite_print_audit(
            {
                "audit_id": details.audit_id,
                "copies": details.document_copies,
                "submitted_at": details.submitted_at,
            },
            list(details.codes),
            Path(details.output_file),
            unifi_ids=(
                list(details.unifi_ids)
                if details.unifi_ids
                else None
            ),
        )
        self.history.finalize_pending_print_audit(details.audit_id)
        return True

    def _deselect_printed_codes(self, codes: list[str]) -> None:
        """Finalize every successful physical-print operation consistently."""

        del codes
        self._finalize_voucher_operation_ui(
            operation="physical_print",
        )

    def _preview(
        self,
        path: Path,
        codes: list[str],
        *,
        site_id: str = "",
        unifi_ids: list[str] | None = None,
        allow_physical_print: bool = True,
    ):
        stable_ids = (
            list(unifi_ids)
            if unifi_ids is not None
            else None
        )
        PdfPreview(
            self,
            path,
            codes,
            self.history,
            self.settings,
            site_id=site_id,
            unifi_ids=stable_ids,
            allow_physical_print=allow_physical_print,
            on_print=self.populate,
            on_audit=lambda pending, audit_codes, pdf_path: (
                self._record_sqlite_print_audit(
                    pending,
                    audit_codes,
                    pdf_path,
                    unifi_ids=stable_ids,
                )
            ),
            on_submitted=lambda: self._deselect_printed_codes(codes),
            confirm_print=lambda parent: self._confirm_physical_reprint(
                codes,
                parent,
                unifi_ids=stable_ids,
            ),
        )

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
                site_id=(
                    self.database.controller_site_id(
                        self.active_controller_id
                    )
                    if self.active_controller_id is not None
                    else ""
                ),
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
                site_id=(
                    self.database.controller_site_id(
                        self.active_controller_id
                    )
                    if self.active_controller_id is not None
                    else ""
                ),
                unifi_ids=(
                    list(resolved.linked_voucher_ids)
                    if resolved.linked_voucher_ids
                    else None
                ),
                allow_physical_print=False,
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
