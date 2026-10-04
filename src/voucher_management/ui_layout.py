"""DPI-safe geometry helpers for modal Windows dialogs."""

from __future__ import annotations


def dialog_size(
    *,
    requested_width: int,
    requested_height: int,
    screen_width: int,
    screen_height: int,
    preferred_width: int,
    preferred_height: int,
    min_width: int,
    min_height: int,
    screen_margin: int = 72,
) -> tuple[int, int]:
    """Return a content-aware size clamped to the usable screen area."""

    max_width = max(320, int(screen_width) - int(screen_margin))
    max_height = max(240, int(screen_height) - int(screen_margin))
    wanted_width = max(
        int(requested_width),
        int(preferred_width),
        int(min_width),
    )
    wanted_height = max(
        int(requested_height),
        int(preferred_height),
        int(min_height),
    )
    return (
        min(wanted_width, max_width),
        min(wanted_height, max_height),
    )


def fit_toplevel_to_content(
    window,
    *,
    preferred_width: int,
    preferred_height: int,
    min_width: int,
    min_height: int,
    screen_margin: int = 72,
) -> tuple[int, int]:
    """Size a Toplevel after layout so DPI/font scaling cannot hide footers."""

    window.update_idletasks()
    width, height = dialog_size(
        requested_width=window.winfo_reqwidth() + 16,
        requested_height=window.winfo_reqheight() + 16,
        screen_width=window.winfo_screenwidth(),
        screen_height=window.winfo_screenheight(),
        preferred_width=preferred_width,
        preferred_height=preferred_height,
        min_width=min_width,
        min_height=min_height,
        screen_margin=screen_margin,
    )
    window.minsize(
        min(int(min_width), width),
        min(int(min_height), height),
    )
    x = max(0, (int(window.winfo_screenwidth()) - width) // 2)
    y = max(0, (int(window.winfo_screenheight()) - height) // 3)
    window.geometry(f"{width}x{height}+{x}+{y}")
    return width, height
