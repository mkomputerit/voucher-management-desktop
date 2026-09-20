"""Official UniFi Network voucher API adapter.

The adapter intentionally exposes the same small controller-independent
ApiVoucher surface consumed by the existing UI/printing workflow. This keeps
the API migration isolated from PDF generation, print audit history and
operator lifecycle policy.

The implementation is based on the documented Network integration API and on
field tests performed against UniFi Network 10.6.106. Authentication uses an
X-API-Key header. API keys are retained only in the client instance memory and
are never persisted by this module.

TLS certificate verification is enabled by default. For a local/self-signed
controller, the client supports explicit SHA-256 certificate pinning: the peer
certificate is matched before any authenticated HTTP request is sent.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


OFFICIAL_LOCAL_API_PATH = "/proxy/network/integration/v1"


class UniFiApiError(RuntimeError):
    """Raised when the documented controller API cannot complete an operation."""


class UniFiCertificateTrustRequired(UniFiApiError):
    """Raised when a self-signed/untrusted certificate needs explicit trust."""

    def __init__(self, fingerprint: str):
        super().__init__(
            "Il certificato TLS del controller non è attendibile. "
            "Verificare e autorizzare esplicitamente la sua impronta SHA-256."
        )
        self.fingerprint = fingerprint


class UniFiCertificateChanged(UniFiApiError):
    """Raised when a previously pinned controller certificate has changed."""

    def __init__(self, previous_fingerprint: str, fingerprint: str):
        super().__init__(
            "Il certificato TLS del controller è cambiato. "
            "Verificare la nuova impronta prima di continuare."
        )
        self.previous_fingerprint = previous_fingerprint
        self.fingerprint = fingerprint


class PinnedCertificateMismatch(OSError):
    """Raised before HTTP headers are sent when a pinned certificate changes."""

    def __init__(self, expected_sha256: str, actual_sha256: str):
        super().__init__("TLS certificate pin mismatch")
        self.expected_sha256 = expected_sha256
        self.actual_sha256 = actual_sha256


def _certificate_sha256(der_certificate: bytes) -> str:
    return hashlib.sha256(der_certificate).hexdigest()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that validates the peer pin before HTTP is sent."""

    def __init__(self, host, *, expected_sha256: str, **kwargs):
        self.expected_sha256 = expected_sha256
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        super().connect()
        if self.sock is None:
            raise PinnedCertificateMismatch(self.expected_sha256, "")
        der = self.sock.getpeercert(binary_form=True)
        actual = _certificate_sha256(der)
        if actual != self.expected_sha256:
            self.close()
            raise PinnedCertificateMismatch(self.expected_sha256, actual)


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject every redirect so API credentials never cross request targets."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(
            req.full_url,
            code,
            "Redirect UniFi rifiutato",
            headers,
            fp,
        )


class _PinnedHTTPSHandler(HTTPSHandler):
    """urllib handler enforcing the fingerprint on the authenticated socket."""

    def __init__(self, expected_sha256: str, context: ssl.SSLContext):
        super().__init__(context=context)
        self.expected_sha256 = expected_sha256

    def https_open(self, req):
        expected = self.expected_sha256

        def connection_factory(host, **kwargs):
            return _PinnedHTTPSConnection(
                host,
                expected_sha256=expected,
                **kwargs,
            )

        return self.do_open(
            connection_factory,
            req,
            context=self._context,
        )


@dataclass(frozen=True)
class ApiVoucher:
    """Voucher fields required by the existing operator/print workflow.

    quota == 0 is an internal compatibility representation for an official
    voucher where authorizedGuestLimit is absent. Zero is never sent to the
    official API, whose documented minimum is 1.
    """

    id: str
    code: str
    recipient: str
    duration_minutes: int
    create_time: int
    quota: int = 0
    used: int = 0
    status: str = ""
    start_time: int = 0
    end_time: int = 0
    status_expires: int = 0
    admin_name: str = ""
    data_mb: int | None = None
    down_kbps: int | None = None
    up_kbps: int | None = None

    @property
    def code_formatted(self) -> str:
        raw = self.code.replace("-", "")
        return f"{raw[:5]}-{raw[5:]}" if len(raw) == 10 else self.code

    @property
    def usage_label(self) -> str:
        if self.quota == 0:
            return f"{self.used} / Illimitato"
        return f"{self.used} / {self.quota}"


def normalize_api_root(value: str) -> str:
    """Return an HTTPS Network API root ending in /v1.

    A bare controller address is accepted only as a migration convenience for
    existing private-beta settings. It expands to the exact local integration
    path observed in the controller's own Network > Integrations documentation.
    Supplying the complete API root remains the preferred configuration.
    """

    raw = value.strip().rstrip("/")
    if not raw:
        raise ValueError("Indirizzo API UniFi mancante")

    if "://" not in raw:
        raw = f"https://{raw}"

    parts = urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise ValueError("L'API UniFi deve usare HTTPS")
    if not parts.hostname:
        raise ValueError("Indirizzo API UniFi non valido")
    if parts.username or parts.password:
        raise ValueError("Non inserire credenziali nell'URL dell'API")
    if parts.query or parts.fragment:
        raise ValueError("L'URL API non deve contenere query o frammenti")

    path = parts.path.rstrip("/")
    if not path:
        path = OFFICIAL_LOCAL_API_PATH
    elif not path.lower().endswith("/v1"):
        raise ValueError(
            "L'URL API deve essere la radice ufficiale che termina in /v1"
        )

    return urlunsplit(("https", parts.netloc, path, "", ""))


def _timestamp_to_epoch(value: object, *, field: str) -> int:
    """Convert a documented RFC3339 date-time to epoch seconds."""

    if value in (None, ""):
        return 0
    try:
        text = str(value)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # OpenAPI date-time values should contain an offset. Treat a
            # timezone-less value as UTC rather than host local time so display
            # and lifecycle decisions remain deterministic.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError) as exc:
        raise UniFiApiError(f"Timestamp UniFi non valido nel campo {field}") from exc


class UniFiClient:
    """Client for the documented UniFi Network integration API."""

    def __init__(
        self,
        api_root: str,
        timeout: int = 15,
        *,
        trusted_cert_sha256: str | None = None,
        preferred_site_id: str | None = None,
    ):
        self.base_url = normalize_api_root(api_root)
        self.timeout = timeout
        self.trusted_cert_sha256 = (
            trusted_cert_sha256.replace(":", "").strip().lower()
            if trusted_cert_sha256 else ""
        )
        if self.trusted_cert_sha256 and (
            len(self.trusted_cert_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.trusted_cert_sha256)
        ):
            raise ValueError("Impronta SHA-256 del certificato non valida")
        self.preferred_site_id = preferred_site_id.strip() if preferred_site_id else ""
        self.site_id = ""
        self.site_name = ""
        self.application_version = ""
        self._api_key = ""

        if self.trusted_cert_sha256:
            # A pinned self-signed certificate is verified by exact SHA-256
            # fingerprint before any authenticated HTTP request is attempted.
            context = ssl._create_unverified_context()
        else:
            context = ssl.create_default_context()
        self._ssl_context = context
        handler = (
            _PinnedHTTPSHandler(self.trusted_cert_sha256, context)
            if self.trusted_cert_sha256
            else HTTPSHandler(context=context)
        )
        self.opener = build_opener(handler, _NoRedirectHandler())

    def _server_certificate_sha256(self) -> str:
        """Fetch the peer certificate without sending HTTP credentials."""

        parts = urlsplit(self.base_url)
        host = parts.hostname
        if not host:
            raise UniFiApiError("Indirizzo API UniFi non valido")
        port = parts.port or 443
        context = ssl._create_unverified_context()
        try:
            with socket.create_connection((host, port), timeout=self.timeout) as raw:
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    der = tls.getpeercert(binary_form=True)
        except (OSError, ssl.SSLError) as exc:
            raise UniFiApiError(
                "Impossibile leggere il certificato TLS del controller"
            ) from exc
        return _certificate_sha256(der)

    @property
    def connected(self) -> bool:
        """Return True only after API-key validation and site discovery."""

        return bool(self._api_key and self.site_id)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        expected: tuple[int, ...] = (200,),
        not_found_message: str | None = None,
    ) -> dict:
        if not self._api_key:
            raise UniFiApiError("Inserire la API key e connettersi prima")

        data = None
        headers = {
            "Accept": "application/json",
            "X-API-Key": self._api_key,
        }
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status = int(response.status)
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            # Do not echo the response body. Voucher responses can contain the
            # activation code and should never leak into UI/log exception text.
            if exc.code == 401:
                raise UniFiApiError("API key UniFi non valida") from exc
            if exc.code == 403:
                raise UniFiApiError(
                    "API key UniFi senza permessi sufficienti per questa operazione"
                ) from exc
            if exc.code == 404:
                raise UniFiApiError(
                    not_found_message
                    or "Endpoint UniFi non disponibile: verificare l'URL API in Network > Integrations"
                ) from exc
            raise UniFiApiError(f"Errore HTTP UniFi {exc.code}") from exc
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, PinnedCertificateMismatch):
                raise UniFiCertificateChanged(
                    reason.expected_sha256,
                    reason.actual_sha256,
                ) from exc
            if isinstance(reason, ssl.SSLCertVerificationError):
                fingerprint = self._server_certificate_sha256()
                raise UniFiCertificateTrustRequired(fingerprint) from exc
            raise UniFiApiError("Controller UniFi non raggiungibile") from exc
        except (TimeoutError, OSError) as exc:
            raise UniFiApiError("Controller UniFi non raggiungibile") from exc

        if status not in expected:
            raise UniFiApiError(f"Risposta HTTP UniFi inattesa: {status}")

        if not raw.strip():
            return {}
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UniFiApiError("Il controller UniFi non ha restituito JSON valido") from exc
        if not isinstance(result, dict):
            raise UniFiApiError("Formato risposta UniFi non valido")
        return result

    @staticmethod
    def _page(result: dict, *, context: str) -> tuple[list[dict], int]:
        """Validate the documented paginated response envelope."""

        required = ("data", "count", "totalCount", "offset", "limit")
        if any(key not in result for key in required):
            raise UniFiApiError(f"{context}: risposta paginata incompleta")
        data = result.get("data")
        if not isinstance(data, list):
            raise UniFiApiError(f"{context}: campo data non valido")
        try:
            total = int(result["totalCount"])
        except (TypeError, ValueError) as exc:
            raise UniFiApiError(f"{context}: totalCount non valido") from exc
        return data, total

    def _list_sites(self) -> list[dict]:
        items: list[dict] = []
        offset = 0
        limit = 200

        while True:
            result = self._request(
                "GET",
                f"/sites?offset={offset}&limit={limit}",
            )
            page, total = self._page(result, context="Elenco siti")
            items.extend(page)
            if len(items) >= total:
                return items
            if not page:
                raise UniFiApiError(
                    "Elenco siti: paginazione incoerente prima di totalCount"
                )
            offset += len(page)

    def connect(self, api_key: str) -> dict:
        """Validate the API key, read Network version and resolve one site.

        The current beta deliberately refuses to guess when multiple sites are
        returned. A future UI can expose explicit site selection without
        changing the voucher adapter itself.
        """

        key = api_key.strip()
        if not key:
            raise UniFiApiError("Inserire la API key UniFi")

        self._api_key = key
        self.site_id = ""
        self.site_name = ""
        self.application_version = ""

        try:
            info = self._request("GET", "/info")
            version = str(info.get("applicationVersion", "")).strip()
            if not version:
                raise UniFiApiError(
                    "La risposta /info non contiene applicationVersion"
                )

            sites = self._list_sites()
            if not sites:
                raise UniFiApiError("Nessun sito UniFi restituito dalla API")

            selected: dict | None = None
            if self.preferred_site_id:
                selected = next(
                    (
                        site
                        for site in sites
                        if str(site.get("id", "")) == self.preferred_site_id
                    ),
                    None,
                )
                if selected is None:
                    raise UniFiApiError("Il Site ID configurato non è disponibile")
            elif len(sites) == 1:
                selected = sites[0]
            else:
                raise UniFiApiError(
                    "La API restituisce più siti. Questa versione richiede "
                    "un controller con selezione del sito univoca; il supporto "
                    "multi-site non è ancora disponibile."
                )

            site_id = str(selected.get("id", "")).strip()
            if not site_id:
                raise UniFiApiError("Il sito UniFi non contiene un ID valido")

            self.application_version = version
            self.site_id = site_id
            self.site_name = str(selected.get("name", "")).strip()
            return {
                "applicationVersion": version,
                "siteId": site_id,
                "siteName": self.site_name,
            }
        except Exception:
            # A failed connection must not leave a usable credential-bearing
            # client behind. The operator can retry with the correct key.
            self._api_key = ""
            self.site_id = ""
            self.site_name = ""
            self.application_version = ""
            raise

    def _resolved_site(self, site: str | None = None) -> str:
        site_id = (site or self.site_id).strip()
        if not site_id:
            raise UniFiApiError("Connettersi prima al controller UniFi")
        return quote(site_id, safe="")

    @staticmethod
    def _voucher_from_json(item: dict) -> ApiVoucher:
        """Map an official voucher detail object to the stable UI model."""

        required = (
            "id",
            "createdAt",
            "name",
            "code",
            "authorizedGuestCount",
            "expired",
            "timeLimitMinutes",
        )
        if any(key not in item for key in required):
            raise UniFiApiError("Voucher UniFi: risposta incompleta")

        voucher_id = str(item.get("id", "")).strip()
        code = str(item.get("code", "")).strip()
        if not voucher_id or not code:
            raise UniFiApiError("Voucher UniFi: ID o codice mancante")

        try:
            duration = int(item["timeLimitMinutes"])
            used = int(item["authorizedGuestCount"])
            quota = (
                int(item["authorizedGuestLimit"])
                if item.get("authorizedGuestLimit") is not None
                else 0
            )
        except (TypeError, ValueError) as exc:
            raise UniFiApiError("Voucher UniFi: contatori non validi") from exc

        expired = bool(item["expired"])
        status = "EXPIRED" if expired else ("USED_MULTIPLE" if used > 0 else "VALID_MULTI")
        start_time = _timestamp_to_epoch(item.get("activatedAt"), field="activatedAt")
        end_time = _timestamp_to_epoch(item.get("expiresAt"), field="expiresAt")

        def optional_int(name: str) -> int | None:
            value = item.get(name)
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise UniFiApiError(
                    f"Voucher UniFi: {name} non valido"
                ) from exc

        return ApiVoucher(
            id=voucher_id,
            code=code,
            recipient=str(item.get("name", "")).strip(),
            duration_minutes=duration,
            create_time=_timestamp_to_epoch(item.get("createdAt"), field="createdAt"),
            quota=quota,
            used=used,
            status=status,
            start_time=start_time,
            end_time=end_time,
            status_expires=end_time,
            data_mb=optional_int("dataUsageLimitMBytes"),
            down_kbps=optional_int("rxRateLimitKbps"),
            up_kbps=optional_int("txRateLimitKbps"),
        )

    def list_vouchers(self, site: str | None = None) -> list[ApiVoucher]:
        """Return all vouchers, following the documented offset pagination."""

        encoded_site = self._resolved_site(site)
        items: list[ApiVoucher] = []
        offset = 0
        limit = 1000

        while True:
            result = self._request(
                "GET",
                f"/sites/{encoded_site}/hotspot/vouchers?offset={offset}&limit={limit}",
            )
            page, total = self._page(result, context="Elenco voucher")
            items.extend(self._voucher_from_json(item) for item in page)
            if len(items) >= total:
                return items
            if not page:
                raise UniFiApiError(
                    "Elenco voucher: paginazione incoerente prima di totalCount"
                )
            offset += len(page)

    def get_voucher(self, voucher_id: str, site: str | None = None) -> ApiVoucher:
        """Read one voucher by its documented UUID endpoint."""

        if not voucher_id.strip():
            raise UniFiApiError("Voucher ID mancante")
        encoded_site = self._resolved_site(site)
        encoded_voucher = quote(voucher_id.strip(), safe="")
        result = self._request(
            "GET",
            f"/sites/{encoded_site}/hotspot/vouchers/{encoded_voucher}",
            not_found_message=(
                "Il voucher non è più presente sul controller UniFi. "
                "Aggiornare l'elenco prima di riprovare."
            ),
        )
        return self._voucher_from_json(result)

    def create_vouchers(
        self,
        recipient: str,
        quantity: int,
        expire_number: int,
        expire_unit: int,
        quota: int,
        data_mb: int | None = None,
        down_mbps: int | None = None,
        up_mbps: int | None = None,
        site: str | None = None,
    ) -> list[ApiVoucher]:
        """Create vouchers using only documented official request fields."""

        recipient = recipient.strip()
        duration = int(expire_number) * int(expire_unit)

        if not recipient:
            raise UniFiApiError("Inserire il nome/nota del voucher")
        if not 1 <= int(quantity) <= 50:
            raise UniFiApiError("Quantità voucher deve essere compresa tra 1 e 50")
        if expire_unit not in (1, 60, 1440) or not 1 <= duration <= 1_000_000:
            raise UniFiApiError("Durata voucher non valida")
        if quota < 0:
            raise UniFiApiError("Numero utilizzi non valido")
        if data_mb is not None and not 1 <= int(data_mb) <= 1_048_576:
            raise UniFiApiError("Limite dati non valido")
        if down_mbps is not None and not 1 <= int(down_mbps) <= 100:
            raise UniFiApiError("Download deve essere compreso tra 1 e 100 Mbps")
        if up_mbps is not None and not 1 <= int(up_mbps) <= 100:
            raise UniFiApiError("Upload deve essere compreso tra 1 e 100 Mbps")

        payload: dict[str, object] = {
            "count": int(quantity),
            "name": recipient,
            "timeLimitMinutes": duration,
        }

        # Official API minimum is 1. Internal quota=0 means "no explicit guest
        # limit", therefore the field is omitted rather than sending zero.
        if quota > 0:
            payload["authorizedGuestLimit"] = int(quota)
        if data_mb is not None:
            payload["dataUsageLimitMBytes"] = int(data_mb)
        if down_mbps is not None:
            payload["rxRateLimitKbps"] = int(down_mbps) * 1000
        if up_mbps is not None:
            payload["txRateLimitKbps"] = int(up_mbps) * 1000

        encoded_site = self._resolved_site(site)
        result = self._request(
            "POST",
            f"/sites/{encoded_site}/hotspot/vouchers",
            payload,
            expected=(201,),
        )
        vouchers = result.get("vouchers")
        if not isinstance(vouchers, list):
            raise UniFiApiError("Creazione voucher: risposta priva di vouchers")
        if len(vouchers) != int(quantity):
            raise UniFiApiError(
                f"Creazione voucher: attesi {quantity}, ricevuti {len(vouchers)}"
            )
        return [self._voucher_from_json(item) for item in vouchers]

    def delete_vouchers(
        self,
        voucher_ids: list[str],
        site: str | None = None,
    ) -> None:
        """Delete selected vouchers one UUID at a time.

        The official API also exposes bulk deletion by filter. Voucher
        Management deliberately avoids it so the controller action matches the
        per-voucher safety decision already made by the UI policy.
        """

        ids = [voucher_id.strip() for voucher_id in voucher_ids if voucher_id.strip()]
        if not ids:
            raise UniFiApiError("Nessun voucher selezionato")

        encoded_site = self._resolved_site(site)
        deleted = 0
        for voucher_id in ids:
            encoded_voucher = quote(voucher_id, safe="")
            try:
                result = self._request(
                    "DELETE",
                    f"/sites/{encoded_site}/hotspot/vouchers/{encoded_voucher}",
                    not_found_message=(
                        "Il voucher è già stato rimosso dal controller UniFi. "
                        "Aggiornare l'elenco prima di riprovare."
                    ),
                )
                count = int(result.get("vouchersDeleted", 0) or 0)
                if count < 1:
                    raise UniFiApiError(
                        "Il controller non ha confermato la cancellazione"
                    )
                deleted += 1
            except (TypeError, ValueError, UniFiApiError) as exc:
                if deleted:
                    raise UniFiApiError(
                        f"Eliminati {deleted} voucher su {len(ids)}; "
                        "operazione interrotta. Aggiornare l'elenco prima di riprovare."
                    ) from exc
                if isinstance(exc, UniFiApiError):
                    raise
                raise UniFiApiError(
                    "Risposta di cancellazione UniFi non valida"
                ) from exc
