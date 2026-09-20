"""Public product identity and neutral defaults.

Keeping these values in one module avoids accidental reintroduction of
installation-specific names, hosts or branding in UI, storage and packaging.
The Python package name is kept temporarily for beta compatibility and will be
renamed only after the public API adapter is field-tested.
"""

PRODUCT_NAME = "Voucher Management"
PRODUCT_DIR_NAME = "VoucherManagement"
LEGACY_PRODUCT_DIR_NAMES = ("UniFiVoucherTool",)

DEFAULT_STRUCTURE_TYPE = "Personalizzata"
DEFAULT_STRUCTURE_NAME = ""
DEFAULT_WIFI_TITLE = "Guest Wi-Fi"
