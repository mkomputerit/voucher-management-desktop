"""Tk adapter for controller connection and TLS trust decisions."""

from __future__ import annotations

from tkinter import messagebox

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
            trusted_pin = saved_pin if saved_root == normalized else ""
            client = UniFiClient(
                normalized,
                trusted_cert_sha256=trusted_pin or None,
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
        """Run one TLS/API connection attempt on the worker thread."""

        def worker():
            info = client.connect(api_key)
            vouchers = client.list_vouchers()
            return client, info, vouchers

        def completed(result) -> None:
            connected_client, info, vouchers = result
            self._finish_connection(
                connected_client,
                info,
                vouchers,
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
        self._show_network_error(
            "UniFi",
            exc,
        )

    def _finish_connection(
        self,
        client: UniFiClient,
        info: dict,
        vouchers,
    ) -> None:
        self.client = client
        self.vouchers = list(vouchers)
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
