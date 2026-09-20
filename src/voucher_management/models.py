"""Controller-independent voucher/PDF data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VoucherRecord:
    code: str
    page_number: int = 0
    duration_minutes: int | None = None
    recipient: str = ""

    @property
    def duration_label(self) -> str:
        minutes = int(self.duration_minutes or 0)
        if minutes and minutes % 1440 == 0:
            days = minutes // 1440
            return f"{days} giorno" if days == 1 else f"{days} giorni"
        if minutes and minutes % 60 == 0:
            hours = minutes // 60
            return f"{hours} ora" if hours == 1 else f"{hours} ore"
        return f"{minutes} minuti" if minutes else "-"

    @property
    def recipient_label(self) -> str:
        return self.recipient.strip() or "Da compilare"


@dataclass
class VoucherBatch:
    source_path: Path
    vouchers: list[VoucherRecord] = field(default_factory=list)
    recipient: str = ""

    @property
    def count(self) -> int:
        return len(self.vouchers)

    @property
    def codes(self) -> list[str]:
        return [v.code for v in self.vouchers]
