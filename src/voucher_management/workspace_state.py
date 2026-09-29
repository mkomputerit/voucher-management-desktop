"""UI-independent operator workspace state for the 5.1 redesign."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ControllerWorkspaceStatus:
    """Concise controller state intended for non-technical operators."""

    key: str
    title: str
    detail: str


def _format_sync_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "mai sincronizzato"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        local = parsed.astimezone()
        return local.strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return "sincronizzazione precedente disponibile"


def build_controller_workspace_status(
    *,
    connected: bool,
    configured: bool,
    controller_name: str = "",
    last_successful_sync_at: str = "",
    busy_label: str = "",
    failed: bool = False,
) -> ControllerWorkspaceStatus:
    """Return a human-facing state without exposing API/TLS implementation detail."""

    name = str(controller_name or "").strip() or "Controller"
    busy = str(busy_label or "").strip()
    if busy:
        return ControllerWorkspaceStatus(
            key="syncing",
            title="Sincronizzazione in corso…",
            detail="Attendi il completamento dell'operazione con il controller.",
        )

    last_sync = _format_sync_time(last_successful_sync_at)
    if failed:
        return ControllerWorkspaceStatus(
            key="error",
            title="Controller non raggiungibile",
            detail=(
                f"I dati locali restano disponibili. Ultimo aggiornamento: "
                f"{last_sync}."
            ),
        )
    if connected:
        return ControllerWorkspaceStatus(
            key="connected",
            title="Pronto",
            detail=f"{name} collegato • ultimo aggiornamento: {last_sync}.",
        )
    if configured:
        return ControllerWorkspaceStatus(
            key="local",
            title="Solo dati locali",
            detail=(
                f"{name} non è collegato in questo momento. Ultimo "
                f"aggiornamento: {last_sync}."
            ),
        )
    return ControllerWorkspaceStatus(
        key="unconfigured",
        title="Controller da configurare",
        detail=(
            "Apri Impostazioni > Controller per completare la connessione "
            "prima di creare o sincronizzare voucher."
        ),
    )
