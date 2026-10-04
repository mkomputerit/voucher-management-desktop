"""Regression tests for DPI-safe modal dialog geometry."""

from __future__ import annotations

from voucher_management.ui_layout import dialog_size, fit_toplevel_to_content


def test_dialog_size_uses_requested_dpi_scaled_content_when_screen_allows():
    assert dialog_size(
        requested_width=1040,
        requested_height=760,
        screen_width=1920,
        screen_height=1080,
        preferred_width=850,
        preferred_height=540,
        min_width=720,
        min_height=460,
    ) == (1040, 760)


def test_dialog_size_clamps_height_on_small_screen_instead_of_hiding_footer():
    width, height = dialog_size(
        requested_width=1020,
        requested_height=900,
        screen_width=1366,
        screen_height=768,
        preferred_width=980,
        preferred_height=590,
        min_width=820,
        min_height=500,
    )

    assert width == 1020
    assert height == 696


class _FakeWindow:
    def __init__(self):
        self.idle_updates = 0
        self.min_size = None
        self.geometry_value = None

    def update_idletasks(self):
        self.idle_updates += 1

    def winfo_reqwidth(self):
        return 1010

    def winfo_reqheight(self):
        return 850

    def winfo_screenwidth(self):
        return 1366

    def winfo_screenheight(self):
        return 768

    def minsize(self, width, height):
        self.min_size = (width, height)

    def geometry(self, value):
        self.geometry_value = value


def test_fit_toplevel_keeps_minimum_safe_and_centers_clamped_dialog():
    window = _FakeWindow()

    size = fit_toplevel_to_content(
        window,
        preferred_width=980,
        preferred_height=590,
        min_width=820,
        min_height=500,
    )

    assert size == (1026, 696)
    assert window.idle_updates == 1
    assert window.min_size == (820, 500)
    assert window.geometry_value == "1026x696+170+24"


def test_dialog_size_never_exceeds_available_screen_margin():
    width, height = dialog_size(
        requested_width=1800,
        requested_height=1200,
        screen_width=1280,
        screen_height=720,
        preferred_width=930,
        preferred_height=560,
        min_width=780,
        min_height=470,
    )

    assert width == 1208
    assert height == 648
