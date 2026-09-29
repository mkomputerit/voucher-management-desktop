"""Operator choices for one backup; passwords and temporary paths are not saved."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from .backup_crypto import validate_backup_password


def validate_backup_directory(value: str, data_root: Path | None = None) -> Path:
    """Require an absolute destination outside the live application archive."""
    if not str(value).strip():
        raise ValueError("Seleziona una cartella per i backup.")
    directory = Path(str(value).strip()).expanduser()
    if not directory.is_absolute():
        raise ValueError("La cartella backup deve avere un percorso completo.")
    directory = directory.resolve()
    if data_root is not None and directory.is_relative_to(Path(data_root).resolve()):
        raise ValueError("Scegli una cartella esterna ai dati dell'applicazione.")
    if directory.exists() and not directory.is_dir():
        raise ValueError("La destinazione selezionata non è una cartella.")
    return directory


def default_backup_directory(app) -> str:
    saved = str(getattr(app, "settings", {}).get("backup_directory") or "").strip()
    return saved or str(app.paths.automatic_backups)


@dataclass(frozen=True)
class BackupChoice:
    """One explicit backup or skip decision, kept only in memory."""
    target: Path | None = None
    password: str | None = None
    skip: bool = False


def make_backup_choice(directory: str, *, protected: bool, password: str,
                       confirmation: str, data_root: Path | None = None) -> BackupChoice:
    folder = validate_backup_directory(directory, data_root)
    secret = None
    if protected:
        secret = validate_backup_password(password)
        if password != confirmation:
            raise ValueError("Le due password non coincidono.")
    suffix = ".vmbk" if secret is not None else ".zip"
    name = "VoucherManagement-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + suffix
    return BackupChoice(folder / name, secret)


class BackupOptionsDialog(tk.Toplevel):
    """Show the default folder, a per-copy override and optional encryption."""
    def __init__(self, parent, *, default_directory: str, closing: bool, data_root=None):
        super().__init__(parent)
        self.result = None
        self.data_root = data_root
        self.directory_var = tk.StringVar(self, value=default_directory)
        self.protected_var = tk.BooleanVar(self, value=True)
        self.password_var = tk.StringVar(self)
        self.confirm_var = tk.StringVar(self)
        self.note_var = tk.StringVar(self)
        self.error_var = tk.StringVar(self)
        self.title("Backup prima di uscire" if closing else "Crea backup")
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text="Vuoi creare una copia di sicurezza prima di uscire?" if closing
                  else "Scegli dove salvare la copia di sicurezza.", wraplength=560).grid(row=0, column=0, sticky="w")
        ttk.Label(body, text=f"Posizione predefinita:\n{default_directory}", wraplength=560).grid(
            row=1, column=0, sticky="w", pady=(12, 8))
        ttk.Entry(body, textvariable=self.directory_var, state="readonly", width=68).grid(row=2, column=0, sticky="ew")
        folders = ttk.Frame(body)
        folders.grid(row=3, column=0, sticky="w", pady=(6, 4))
        ttk.Button(folders, text="Scegli un'altra cartella…", command=self.choose_directory).pack(side="left")
        ttk.Button(folders, text="Usa predefinita", command=lambda: self.directory_var.set(default_directory)).pack(side="left", padx=8)
        ttk.Label(body, text="La cartella scelta qui vale solo per questa copia.", wraplength=560).grid(row=4, column=0, sticky="w")
        ttk.Checkbutton(body, text="Proteggi con password (consigliato, facoltativo)", variable=self.protected_var,
                        command=self._toggle_password).grid(row=5, column=0, sticky="w", pady=(14, 6))
        secret = ttk.Frame(body)
        secret.grid(row=6, column=0, sticky="ew")
        secret.columnconfigure(1, weight=1)
        self.password_entries = []
        for row, (label, variable) in enumerate((("Password", self.password_var), ("Ripeti password", self.confirm_var))):
            ttk.Label(secret, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12))
            entry = ttk.Entry(secret, textvariable=variable, show="•", width=34)
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            self.password_entries.append(entry)
        ttk.Label(body, textvariable=self.note_var, wraplength=560).grid(row=7, column=0, sticky="w", pady=(6, 0))
        ttk.Label(body, textvariable=self.error_var, wraplength=560).grid(row=8, column=0, sticky="w", pady=4)
        actions = ttk.Frame(body)
        actions.grid(row=9, column=0, sticky="e", pady=(12, 0))
        ttk.Button(actions, text="Annulla", command=self.destroy).pack(side="left", padx=4)
        if closing:
            ttk.Button(actions, text="Esci senza backup", command=self.skip).pack(side="left", padx=4)
        ttk.Button(actions, text="Crea backup e chiudi" if closing else "Crea backup",
                   command=self.accept, style="Accent.TButton").pack(side="left", padx=4)
        self._toggle_password()
        self.update_idletasks()
        # Keep the dialog content-sized on high-DPI desktops.
        self.geometry(f"{self.winfo_reqwidth()}x{self.winfo_reqheight()}")
        self.grab_set()

    def choose_directory(self):
        selected = filedialog.askdirectory(parent=self, title="Cartella per questa copia",
                                           initialdir=self.directory_var.get(), mustexist=False)
        if selected:
            self.directory_var.set(selected)

    def _toggle_password(self):
        enabled = self.protected_var.get()
        for entry in self.password_entries:
            entry.state(["!disabled"] if enabled else ["disabled"])
        if not enabled:
            self.password_var.set("")
            self.confirm_var.set("")
        self.note_var.set("Backup cifrato .vmbk: almeno 12 caratteri. Conserva la password: non viene salvata."
                          if enabled else "Backup ZIP non cifrato: chi accede al file può leggere dati, codici voucher e PDF.")

    def accept(self):
        try:
            self.result = make_backup_choice(self.directory_var.get(), protected=self.protected_var.get(),
                                             password=self.password_var.get(), confirmation=self.confirm_var.get(),
                                             data_root=self.data_root)
        except (ValueError, OSError) as exc:
            self.error_var.set(str(exc))
            return
        self.destroy()

    def skip(self):
        self.result = BackupChoice(skip=True)
        self.destroy()

    def destroy(self):
        self.password_var.set("")
        self.confirm_var.set("")
        super().destroy()


def ask_backup_options(parent, *, default_directory: str, closing: bool, data_root=None):
    previous_grab = parent.grab_current()
    dialog = BackupOptionsDialog(parent, default_directory=default_directory, closing=closing, data_root=data_root)
    parent.wait_window(dialog)
    if previous_grab is not None:
        try:
            if previous_grab.winfo_exists():
                previous_grab.grab_set()
        except tk.TclError:
            pass
    return dialog.result
