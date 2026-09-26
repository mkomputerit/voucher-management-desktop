"""Reusable Tk dialogs for Voucher Management operator workflows."""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from .backup_crypto import validate_backup_password
from .pdf_render import VOUCHERS_PER_PAGE
from .reprint_policy import ReprintWarning
from .unifi_api import ApiVoucher
from .workflows import validate_create_params


def validate_password_entry(
    password: str,
    confirmation: str | None = None,
) -> str:
    """Apply the crypto-layer password policy and optional confirmation."""

    validated = validate_backup_password(password)
    if confirmation is not None and confirmation != validated:
        raise ValueError("Le due password non coincidono.")
    return validated


class CreateDialog(tk.Toplevel):
    """Collect the controller parameters required to create a voucher batch."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Aggiungi nuovo voucher")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.name = tk.StringVar()
        self.qty = tk.IntVar(value=1)
        self.mode = tk.StringVar(value="Monouso")
        self.quota = tk.IntVar(value=2)
        self.expire = tk.IntVar(value=24)
        self.unit = tk.StringVar(value="Ore")
        self.data = tk.StringVar()
        self.down = tk.StringVar()
        self.up = tk.StringVar()

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        for row, (label, widget) in enumerate(
            (
                ("Nome", ttk.Entry(frame, textvariable=self.name, width=34)),
                (
                    "Quantità",
                    ttk.Spinbox(
                        frame,
                        from_=1,
                        to=50,
                        textvariable=self.qty,
                        width=8,
                    ),
                ),
            )
        ):
            ttk.Label(frame, text=label).grid(
                row=row,
                column=0,
                sticky="w",
                pady=4,
            )
            widget.grid(
                row=row,
                column=1,
                sticky="ew",
                pady=4,
            )

        ttk.Label(frame, text="Utilizzo").grid(
            row=2,
            column=0,
            sticky="w",
            pady=4,
        )
        ttk.Combobox(
            frame,
            textvariable=self.mode,
            state="readonly",
            values=("Monouso", "Multiuso", "Multiuso illimitato"),
            width=22,
        ).grid(row=2, column=1, sticky="w")

        ttk.Label(
            frame,
            text="Numero utilizzi (Multiuso)",
        ).grid(row=3, column=0, sticky="w", pady=4)
        ttk.Spinbox(
            frame,
            from_=2,
            to=999,
            textvariable=self.quota,
            width=8,
        ).grid(row=3, column=1, sticky="w")

        ttk.Label(frame, text="Scadenza").grid(
            row=4,
            column=0,
            sticky="w",
            pady=4,
        )
        expiry = ttk.Frame(frame)
        expiry.grid(row=4, column=1, sticky="w")
        ttk.Spinbox(
            expiry,
            from_=1,
            to=9999,
            textvariable=self.expire,
            width=8,
        ).pack(side="left")
        ttk.Combobox(
            expiry,
            textvariable=self.unit,
            state="readonly",
            values=("Minuti", "Ore", "Giorni"),
            width=10,
        ).pack(side="left", padx=6)

        for row, (label, variable) in enumerate(
            (
                ("Limite dati MB (vuoto = illimitato)", self.data),
                ("Download Mbps (vuoto = illimitato)", self.down),
                ("Upload Mbps (vuoto = illimitato)", self.up),
            ),
            5,
        ):
            ttk.Label(frame, text=label).grid(
                row=row,
                column=0,
                sticky="w",
                pady=4,
            )
            ttk.Entry(
                frame,
                textvariable=variable,
                width=12,
            ).grid(row=row, column=1, sticky="w")

        buttons = ttk.Frame(frame)
        buttons.grid(
            row=8,
            column=0,
            columnspan=2,
            sticky="e",
            pady=(14, 0),
        )
        ttk.Button(
            buttons,
            text="Annulla",
            command=self.destroy,
        ).pack(side="left", padx=5)
        ttk.Button(
            buttons,
            text="Aggiungi",
            command=self.accept,
        ).pack(side="left")
        self.wait_window(self)

    def accept(self) -> None:
        try:
            self.result = validate_create_params(
                recipient=self.name.get(),
                quantity=self.qty.get(),
                mode=self.mode.get(),
                quota=self.quota.get(),
                expire_number=self.expire.get(),
                expire_unit=self.unit.get(),
                data_mb=self.data.get(),
                down_mbps=self.down.get(),
                up_mbps=self.up.get(),
            )
        except (TypeError, ValueError, tk.TclError):
            messagebox.showerror(
                "Voucher",
                "Controllare i valori inseriti",
                parent=self,
            )
            return
        self.destroy()


class PrintCopiesDialog(tk.Toplevel):
    """Ask physical-label count for one unlimited voucher."""

    def __init__(self, parent, voucher: ApiVoucher):
        super().__init__(parent)
        self.title("Copie voucher")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.copies = tk.IntVar(value=1)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=(
                f"Voucher {voucher.code_formatted} - utilizzo illimitato"
            ),
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="Numero di copie fisiche da preparare:",
        ).pack(anchor="w", pady=(10, 4))
        ttk.Spinbox(
            frame,
            from_=1,
            to=999,
            textvariable=self.copies,
            width=8,
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                f"Massimo {VOUCHERS_PER_PAGE} voucher per pagina; "
                "le pagine aggiuntive sono automatiche."
            ),
            foreground="#666",
        ).pack(anchor="w", pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e", pady=(14, 0))
        ttk.Button(
            buttons,
            text="Annulla",
            command=self.destroy,
        ).pack(side="left", padx=4)
        ttk.Button(
            buttons,
            text="Continua",
            command=self.accept,
        ).pack(side="left")
        self.wait_window(self)

    def accept(self) -> None:
        try:
            copies = int(self.copies.get())
            if not 1 <= copies <= 999:
                raise ValueError("copies out of range")
        except (TypeError, ValueError, tk.TclError):
            messagebox.showerror(
                "Copie",
                "Inserire un numero tra 1 e 999",
                parent=self,
            )
            return
        self.result = copies
        self.destroy()


def _local_print_time(value: str) -> str:
    """Format stored ISO print time for an operator-facing confirmation."""

    text = str(value or "").strip()
    if not text:
        return "-"
    try:
        return (
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%d/%m/%Y %H:%M")
        )
    except ValueError:
        return text


class ReprintConfirmDialog(tk.Toplevel):
    """Require explicit confirmation before a duplicate physical print."""

    def __init__(
        self,
        parent,
        warnings: list[tuple[str, ReprintWarning]],
    ):
        super().__init__(parent)
        self.result = False
        self.warnings = list(warnings)
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)

        if len(self.warnings) == 1:
            self.title("Questo voucher è già stato stampato")
            code, warning = self.warnings[0]
            ttk.Label(
                frame,
                text="Questo voucher è già stato stampato",
                font=("TkDefaultFont", 11, "bold"),
            ).pack(anchor="w")
            ttk.Label(
                frame,
                text=(
                    "Stai per generare una copia dello stesso voucher. "
                    "Procedi solo se la copia precedente è stata smarrita "
                    "o se sei certo che sia necessaria una ristampa."
                ),
                wraplength=520,
                justify="left",
            ).pack(anchor="w", pady=(10, 12))
            ttk.Label(
                frame,
                text=f"Voucher: {code}",
            ).pack(anchor="w")
            ttk.Label(
                frame,
                text=(
                    "Ultima stampa: "
                    f"{_local_print_time(warning.last_printed_at)}"
                ),
            ).pack(anchor="w", pady=(4, 0))
            ttk.Label(
                frame,
                text=(
                    "Stampe precedenti: "
                    f"{warning.previous_print_jobs}"
                ),
            ).pack(anchor="w", pady=(4, 0))
            continue_text = "Ristampa voucher"
        else:
            self.title("Conferma ristampe")
            count = len(self.warnings)
            ttk.Label(
                frame,
                text="La selezione contiene voucher già stampati",
                font=("TkDefaultFont", 11, "bold"),
            ).pack(anchor="w")
            ttk.Label(
                frame,
                text=(
                    f"La selezione contiene {count} voucher già stampati. "
                    "Continuando verranno generate copie di voucher "
                    "precedentemente stampati."
                ),
                wraplength=560,
                justify="left",
            ).pack(anchor="w", pady=(10, 10))

            self.details = ttk.Frame(frame)
            self.details_visible = False
            details_button = ttk.Button(
                frame,
                text=f"Visualizza i {count} voucher",
                command=lambda: self._toggle_details(details_button),
            )
            details_button.pack(anchor="w", pady=(0, 8))

            for code, warning in self.warnings:
                ttk.Label(
                    self.details,
                    text=(
                        f"{code} — {warning.previous_print_jobs} stampe — "
                        f"ultima {_local_print_time(warning.last_printed_at)}"
                    ),
                ).pack(anchor="w", pady=2)
            continue_text = "Continua con le ristampe"

        actions = ttk.Frame(frame)
        actions.pack(anchor="e", pady=(16, 0))
        cancel = ttk.Button(
            actions,
            text="Annulla",
            command=self.destroy,
        )
        cancel.pack(side="left", padx=(0, 8))
        ttk.Button(
            actions,
            text=continue_text,
            command=self._accept,
        ).pack(side="left")

        self.bind("<Escape>", lambda _event: self.destroy())
        cancel.focus_set()
        self.wait_window(self)

    def _toggle_details(self, button) -> None:
        self.details_visible = not self.details_visible
        if self.details_visible:
            self.details.pack(fill="x", anchor="w", pady=(0, 8))
            button.configure(text="Nascondi elenco")
        else:
            self.details.pack_forget()
            button.configure(text=f"Visualizza i {len(self.warnings)} voucher")

    def _accept(self) -> None:
        self.result = True
        self.destroy()


class PasswordDialog(tk.Toplevel):
    """Modal password prompt with optional confirmation and reveal toggle."""

    def __init__(
        self,
        parent,
        *,
        title: str,
        prompt: str,
        confirm: bool = False,
    ):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: str | None = None

        self.password_var = tk.StringVar()
        self.confirm_var = tk.StringVar()
        self.show_var = tk.BooleanVar(value=False)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=prompt,
            wraplength=460,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(frame, text="Password").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(12, 4),
        )
        self.password_entry = ttk.Entry(
            frame,
            textvariable=self.password_var,
            show="•",
            width=42,
        )
        self.password_entry.grid(
            row=1,
            column=1,
            sticky="ew",
            pady=(12, 4),
        )

        row = 2
        self.confirm_entry = None
        if confirm:
            ttk.Label(
                frame,
                text="Conferma password",
            ).grid(row=row, column=0, sticky="w", pady=4)
            self.confirm_entry = ttk.Entry(
                frame,
                textvariable=self.confirm_var,
                show="•",
                width=42,
            )
            self.confirm_entry.grid(
                row=row,
                column=1,
                sticky="ew",
                pady=4,
            )
            row += 1

        ttk.Checkbutton(
            frame,
            text="Mostra password",
            variable=self.show_var,
            command=self._toggle_visibility,
        ).grid(
            row=row,
            column=1,
            sticky="w",
            pady=(6, 0),
        )
        row += 1

        actions = ttk.Frame(frame)
        actions.grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="e",
            pady=(14, 0),
        )
        ttk.Button(
            actions,
            text="Annulla",
            command=self.destroy,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            actions,
            text="Continua",
            command=self.accept,
        ).pack(side="left")

        frame.columnconfigure(1, weight=1)
        self.bind("<Return>", lambda _event: self.accept())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.password_entry.focus_set()
        self.wait_window(self)

    def _toggle_visibility(self) -> None:
        show = "" if self.show_var.get() else "•"
        self.password_entry.configure(show=show)
        if self.confirm_entry is not None:
            self.confirm_entry.configure(show=show)

    def accept(self) -> None:
        confirmation = (
            self.confirm_var.get()
            if self.confirm_entry is not None
            else None
        )
        try:
            self.result = validate_password_entry(
                self.password_var.get(),
                confirmation,
            )
        except ValueError as exc:
            messagebox.showerror(
                "Password",
                str(exc),
                parent=self,
            )
            return
        self.destroy()


def ask_password(
    parent,
    *,
    title: str,
    prompt: str,
    confirm: bool = False,
) -> str | None:
    """Return a validated password or None when the operator cancels."""

    return PasswordDialog(
        parent,
        title=title,
        prompt=prompt,
        confirm=confirm,
    ).result
