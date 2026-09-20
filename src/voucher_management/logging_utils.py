"""Privacy-conscious diagnostic logging for support and third-party review."""

from __future__ import annotations

import logging
import platform
import time
from pathlib import Path


LOGGER_NAME = "voucher_management"


def configure_logging(log_dir: Path, retention_days: int = 30) -> logging.Logger:
    """Create a local rotating-by-date diagnostic log without host identity.

    Logs intentionally avoid usernames, hostnames, controller addresses,
    voucher codes and authentication material so they are safer to share during
    support or third-party review.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - max(1, retention_days) * 86400
    for item in log_dir.glob("app-*.log"):
        try:
            if item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            pass

    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    filename = log_dir / f"app-{time.strftime('%Y%m%d')}.log"
    handler = logging.FileHandler(filename, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    logger.info(
        "startup os=%s arch=%s",
        platform.system(),
        platform.machine(),
    )
    return logger
