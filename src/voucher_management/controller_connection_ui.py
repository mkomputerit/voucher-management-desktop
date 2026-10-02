"""Tk adapter for controller connection and TLS trust decisions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox

from .create_reporting_recovery import reconcile_pending_create_reporting_to_path
from .sync_store import (
    PersistedControllerSnapshot,
    persist_connection_snapshot_to_path,
    persist_successful_snapshot,
)

from .unifi_api import (
    UniFiApiError,
    UniFiCertificateChanged,
    UniFiCertificateTrustRequired,
    UniFiClient,
    normalize_api_root,
)


class ControllerConnectionMixin:
    """Non-layout controller connection workflow for the Windows UI."""

    @staticmethod
    def _format_certificate_fingerprint(value: str) -> str:
        compact = value.replace(":", "").strip().upper()
        return ":".join(
            compact[index:index + 2]
            for index in range(0, len(compact), 2)
        )

    def connect(self) -> None:
        """Start a non-blocking official-API connection attempt."""

        if self._background_results is not None:
            self.bell()
            return

        api_root = self.api_root_var.get().strip()
        api_key = self.api_key_var.get()
        name_var = getattr(self, "controller_name_var", None)
        self._requested_controller_name = (
            str(name_var.get()).strip()
            if name_var is not None
            else ""
        )
        # Clear the visible secret before any network operation starts. The
        # captured value exists only in memory for the active worker chain.
        self.api_key_var.set("")

        try:
            normalized = normalize_api_root(api_root)
            # Always resolve persisted trust from the latest on-disk state.
            self.settings = self.settings_store.load()
            saved_root = str(
                self.settings.get("controller_api_root", "")
            ).strip()
            saved_pin = str(
                self.settings.get("controller_cert_sha256", "")
            ).strip()
            saved_site_id = str(
                self.settings.get("controller_site_id", "")
            ).strip()
            trusted_pin = saved_pin if saved_root == normalized else ""
            preferred_site_id = (
                saved_site_id
                if saved_root == normalized and saved_site_id
                else None
            )
            client = UniFiClient(
                normalized,
                trusted_cert_sha256=trusted_pin or None,
                preferred_site_id=preferred_site_id,
            )
        except (UniFiApiError, ValueError) as exc:
            self._connection_failed(exc)
            return

        self.connection_var.set("Connessione in corso…")
        self._start_connect_attempt(client, api_key)

    def _start_connect_attempt(
        self,
        client: UniFiClient,
        api_key: str,
    ) -> None:
        """Connect, fetch and persist one snapshot entirely off the Tk thread."""

        database_path = Path(self.paths.database)
        requested_name = str(
            getattr(self, "_requested_controller_name", "") or ""
        ).strip()

        def worker():
            info = client.connect(api_key)
            vouchers = list(client.list_vouchers())
            observed_at = datetime.now(timezone.utc).isoformat()
            persisted = None
            archive_error = None
            try:
                persisted = persist_connection_snapshot_to_path(
                    database_path,
                    api_root=client.base_url,
                    cert_sha256=client.trusted_cert_sha256 or "",
                    requested_name=requested_name,
                    site_name=str(info.get("siteName") or ""),
                    site_id=str(info.get("siteId") or ""),
                    vouchers=vouchers,
                    observed_at=observed_at,
                )
                marker_path = getattr(
                    self.paths,
                    "pending_create_reporting",
                    database_path.with_name("pending_create_reporting.json"),
                )
                reconcile_pending_create_reporting_to_path(
                    database_path,
                    marker_path,
                    controller_id=persisted.controller_id,
                )
            except Exception as exc:
                archive_error = exc
            return client, info, vouchers, persisted, archive_error

        def completed(result) -> None:
            connected_client, info, vouchers, persisted, archive_error = result
            self._finish_connection(
                connected_client,
                info,
                vouchers,
                persisted=persisted,
                archive_error=archive_error,
            )

        def failed(exc: Exception) -> None:
            if isinstance(exc, UniFiCertificateChanged):
                self._confirm_changed_certificate(
                    client.base_url,
                    api_key,
                    exc,
                )
                return
            if isinstance(exc, UniFiCertificateTrustRequired):
                self._confirm_untrusted_certificate(
                    client.base_url,
                    api_key,
                    exc,
                )
                return
            self._connection_failed(exc)

        self._run_network_task(
            "Connessione al controller UniFi…",
            worker,
            completed,
            failed,
        )

    def _confirm_changed_certificate(
        self,
        api_root: str,
        api_key: str,
        exc: UniFiCertificateChanged,
    ) -> None:
        """Ask for changed-certificate approval only from the Tk thread."""

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
            self._connection_failed(
                UniFiApiError(
                    "Nuovo certificato TLS non autorizzato dall'operatore"
                )
            )
            return

        try:
            client = UniFiClient(
                api_root,
                trusted_cert_sha256=exc.fingerprint,
            )
        except ValueError as error:
            self._connection_failed(error)
            return
        self.connection_var.set("Connessione in corso…")
        self._start_connect_attempt(client, api_key)

    def _confirm_untrusted_certificate(
        self,
        api_root: str,
        api_key: str,
        exc: UniFiCertificateTrustRequired,
    ) -> None:
        """Ask for first-use certificate approval only from the Tk thread."""

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
            self._connection_failed(
                UniFiApiError(
                    "Certificato TLS non autorizzato dall'operatore"
                )
            )
            return

        try:
            client = UniFiClient(
                api_root,
                trusted_cert_sha256=exc.fingerprint,
            )
        except ValueError as error:
            self._connection_failed(error)
            return
        self.connection_var.set("Connessione in corso…")
        self._start_connect_attempt(client, api_key)

    def _connection_failed(self, exc: Exception) -> None:
        self.client = None
        self.connection_var.set("Connessione non riuscita")
        callback = getattr(self, "_controller_operation_failed", None)
        if callback is not None:
            callback()
        self._show_network_error(
            "UniFi",
            exc,
        )

    def _finish_connection(
        self,
        client: UniFiClient,
        info: dict,
        vouchers,
        *,
        profile_name: str | None = None,
        observed_at: str | None = None,
        persisted: PersistedControllerSnapshot | None = None,
        archive_error: Exception | None = None,
    ) -> None:
        """Apply one connection result after durable worker persistence.

        Normal operator connections pass a persisted result and therefore
        perform no bulk SQLite work on the Tk thread. The synchronous fallback
        remains only for onboarding/tests that explicitly call this method.
        """

        snapshot = list(vouchers)
        snapshot_observed_at = (
            persisted.observed_at
            if persisted is not None
            else (
                str(observed_at).strip()
                if observed_at and str(observed_at).strip()
                else datetime.now(timezone.utc).isoformat()
            )
        )

        if persisted is None:
            requested_name = (
                str(profile_name).strip()
                if profile_name and str(profile_name).strip()
                else str(
                    getattr(self, "_requested_controller_name", "")
                ).strip()
            )
            existing_id = self.database.find_controller_by_identity(
                api_root=client.base_url,
                site_id=str(info.get("siteId") or ""),
            )
            persisted_name = requested_name
            if not persisted_name and existing_id is not None:
                persisted_name = self.database.controller_name(existing_id) or ""
            if not persisted_name:
                persisted_name = str(
                    info.get("siteName") or "Controller UniFi"
                )

            if archive_error is None:
                controller_id = self.database.get_or_create_controller(
                    name=persisted_name,
                    api_root=client.base_url,
                    observed_at=snapshot_observed_at,
                    cert_sha256=client.trusted_cert_sha256 or "",
                    site_id=str(info.get("siteId") or ""),
                )
                persist_successful_snapshot(
                    self.database,
                    controller_id=controller_id,
                    vouchers=snapshot,
                    observed_at=snapshot_observed_at,
                )
            else:
                # The network side is healthy; do not retry SQLite on Tk or
                # misreport this as a controller failure.
                controller_id = existing_id
        else:
            controller_id = persisted.controller_id
            persisted_name = persisted.controller_name

        self.active_controller_id = controller_id
        self.client = client
        self.vouchers = snapshot
        snapshot_authoritative = not bool(
            persisted is not None and persisted.suspected_absence_ids
        )
        self.controller_snapshot_live = snapshot_authoritative
        self.api_root_var.set(client.base_url)
        name_var = getattr(self, "controller_name_var", None)
        if name_var is not None:
            name_var.set(persisted_name)
        self.settings = self.settings_store.update(
            controller_api_root=client.base_url,
            controller_site_id=str(info.get("siteId") or ""),
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
        if archive_error is None and snapshot_authoritative:
            callback = getattr(self, "_controller_operation_succeeded", None)
            if callback is not None:
                callback()
        elif archive_error is None:
            callback = getattr(self, "_controller_operation_stale", None)
            if callback is not None:
                callback()
        else:
            self.logger.error(
                "connection_archive_persistence_failed type=%s",
                type(archive_error).__name__,
            )
            callback = getattr(self, "_controller_operation_stale", None)
            if callback is not None:
                callback(archive_failed=True)
            messagebox.showwarning(
                "Controller collegato • archivio locale da verificare",
                "La connessione UniFi è riuscita e i dati Home sono live, ma "
                "lo storico locale non è stato aggiornato. I Report potrebbero "
                "essere incompleti finché una sincronizzazione non riesce.",
                parent=self,
            )
        self.populate()
        if snapshot_authoritative:
            recovery = getattr(
                self,
                "_offer_uncertain_create_recovery_after_refresh",
                None,
            )
            if callable(recovery):
                recovery(snapshot)

        self.logger.info(
            "controller_api_connected network_version=%s tls_pinned=%s "
            "snapshot_persisted_off_ui=%s snapshot_authoritative=%s",
            info["applicationVersion"],
            bool(client.trusted_cert_sha256),
            persisted is not None,
            snapshot_authoritative,
        )
        after = getattr(self, "after", None)
        if callable(after):
            after(
                300,
                lambda: self.logger.info(
                    "controller_ui_event_loop_ready"
                ),
            )
