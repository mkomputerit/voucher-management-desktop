"""Pure first-run onboarding state and commit boundary.

API keys are intentionally absent from every dataclass and persistence method in
this module. Network credentials exist only in the active Tk/client session.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .database import Database
from .identity import DEFAULT_STRUCTURE_TYPE, DEFAULT_WIFI_TITLE
from .retention import RETENTION_INTRO_KEY
from .settings import DEFAULT_SETTINGS, SettingsStore


DEFAULT_VOUCHER_RETENTION_DAYS = 180
ONBOARDING_IN_PROGRESS_KEY = "onboarding_in_progress"
LEGACY_MIGRATION_DECLINED_KEY = "shared_legacy_migration_declined"


class OnboardingState(str, Enum):
    """Startup disposition without making any network request."""

    REQUIRED = "required"
    COMPLETE = "complete"
    EXISTING_INSTALLATION = "existing_installation"
    MIGRATION_AVAILABLE = "migration_available"


@dataclass(frozen=True)
class OnboardingDraft:
    """Non-secret configuration collected by the first-run wizard."""

    installation_name: str
    description: str = ""
    structure_type: str = DEFAULT_STRUCTURE_TYPE
    structure_name: str = ""
    wifi_title: str = DEFAULT_WIFI_TITLE
    logo_path: str = ""
    pdf_title: str = ""
    pdf_subtitle: str = ""
    pdf_contact: str = ""
    pdf_notes: str = ""
    unused_unprinted_days: int = DEFAULT_VOUCHER_RETENTION_DAYS


def legacy_installation_has_evidence(paths, settings: dict) -> bool:
    """Detect pre-5 persistent data without treating bootstrap files as usage.

    A public 4.x installation can have settings/history/PDF/logo data but no
    SQLite database. Such an upgrade must never be forced through the wizard
    for a genuinely new 5.0 installation. The history-key fingerprint is
    ignored because a fresh 5.0 startup creates that bootstrap identity before
    onboarding is scheduled.
    """

    for key, default in DEFAULT_SETTINGS.items():
        if key == "history_key_fingerprint":
            continue
        if settings.get(key, default) != default:
            return True

    history_value = getattr(paths, "history", None)
    if history_value is not None:
        history = Path(history_value)
        try:
            if history.is_file() and history.stat().st_size > 0:
                return True
        except OSError:
            # Unreadable legacy evidence is not a safe reason to classify the
            # installation as new.
            return True

    for attribute in ("prints", "logos"):
        folder_value = getattr(paths, attribute, None)
        if folder_value is None:
            continue
        folder = Path(folder_value)
        try:
            if folder.is_dir() and any(
                item.is_file() for item in folder.rglob("*")
            ):
                return True
        except OSError:
            return True
    return False


def onboarding_state(database: Database) -> OnboardingState:
    """Classify startup conservatively.

    A singleton installation profile is the completion marker. Existing
    operational/migration data without that marker identifies an upgrade or
    restored installation and must never be forced through a 'new install'
    wizard automatically.
    """

    if database.installation_profile() is not None:
        return OnboardingState.COMPLETE
    if database.metadata_value(ONBOARDING_IN_PROGRESS_KEY) == "1":
        return OnboardingState.REQUIRED
    if database.onboarding_has_operational_data():
        return OnboardingState.EXISTING_INSTALLATION
    return OnboardingState.REQUIRED


def begin_onboarding(database: Database) -> None:
    """Persist a retry marker before any controller snapshot can be written."""

    database.set_metadata_value(ONBOARDING_IN_PROGRESS_KEY, "1")


def validate_onboarding_draft(draft: OnboardingDraft) -> OnboardingDraft:
    """Validate only fields that can be persisted by onboarding."""

    installation_name = draft.installation_name.strip()
    structure_name = draft.structure_name.strip()
    wifi_title = draft.wifi_title.strip()
    structure_type = draft.structure_type.strip() or DEFAULT_STRUCTURE_TYPE
    if not installation_name:
        raise ValueError("Inserire un nome per questa installazione")
    if not structure_name:
        raise ValueError("Inserire il nome della struttura")
    if not wifi_title:
        raise ValueError("Inserire il titolo Wi-Fi")
    days = int(draft.unused_unprinted_days)
    if not 1 <= days <= 3650:
        raise ValueError(
            "La retention voucher deve essere compresa tra 1 e 3650 giorni"
        )
    return OnboardingDraft(
        installation_name=installation_name,
        description=draft.description.strip(),
        structure_type=structure_type,
        structure_name=structure_name,
        wifi_title=wifi_title,
        logo_path=draft.logo_path.strip(),
        pdf_title=draft.pdf_title.strip(),
        pdf_subtitle=draft.pdf_subtitle.strip(),
        pdf_contact=draft.pdf_contact.strip(),
        pdf_notes=draft.pdf_notes.strip(),
        unused_unprinted_days=days,
    )


def complete_onboarding(
    database: Database,
    settings_store: SettingsStore,
    draft: OnboardingDraft,
    *,
    observed_at: str,
) -> dict:
    """Persist onboarding with the profile row written last as completion marker.

    settings.json is atomic by itself but cannot share a transaction with
    SQLite. Write it first; then write retention and installation profile in one
    SQLite transaction. If SQLite fails, no completion marker exists and the
    wizard safely repeats on the next launch rather than pretending setup
    finished.
    """

    clean = validate_onboarding_draft(draft)

    settings = settings_store.update(
        structure_type=clean.structure_type,
        structure_name=clean.structure_name,
        wifi_title=clean.wifi_title,
        logo_path=clean.logo_path,
    )

    with database.transaction() as db:
        database.upsert_retention_policy(
            unused_unprinted_days=clean.unused_unprinted_days,
            observed_at=observed_at,
            connection=db,
        )
        db.execute(
            """INSERT INTO settings(key, value, updated_at)
               VALUES (?, '1', ?)
               ON CONFLICT(key) DO UPDATE SET value='1', updated_at=excluded.updated_at""",
            (RETENTION_INTRO_KEY, observed_at),
        )
        database.upsert_installation_profile(
            installation_name=clean.installation_name,
            description=clean.description,
            logo_filename=(
                Path(clean.logo_path).name if clean.logo_path else ""
            ),
            pdf_title=clean.pdf_title,
            pdf_subtitle=clean.pdf_subtitle,
            pdf_contact=clean.pdf_contact,
            pdf_notes=clean.pdf_notes,
            observed_at=observed_at,
            connection=db,
        )
        database.delete_metadata_value(
            ONBOARDING_IN_PROGRESS_KEY,
            connection=db,
        )
    return settings
