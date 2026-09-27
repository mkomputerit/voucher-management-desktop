"""Tk dialog for exporting printable/privacy-safe 5.0 reports."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .report_render import render_report_csv, render_report_pdf
from .reporting import ReportKind, build_report_dataset


REPORT_CHOICES = (
    ("Riepilogo", ReportKind.SUMMARY),
    ("Voucher utilizzati", ReportKind.USED),
    ("Voucher scaduti", ReportKind.EXPIRED),
    ("Stampati mai utilizzati", ReportKind.PRINTED_UNUSED),
    ("Mai stampati", ReportKind.NEVER_PRINTED),
    ("Nominali", ReportKind.NOMINAL),
    ("Storico completo", ReportKind.FULL_HISTORY),
)
REPORT_KIND_BY_LABEL = dict(REPORT_CHOICES)


class ReportDialog(tk.Toplevel):
    """Small operator-facing report export workflow."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Report")
        self.transient(app)
        self.grab_set()
        self._busy = False
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.kind_var = tk.StringVar(value=REPORT_CHOICES[0][0])
        self.scope_var = tk.StringVar(
            value=(
                "Controller attivo"
                if getattr(app, "active_controller_id", None) is not None
                else "Tutti i controller"
            )
        )
        self.format_var = tk.StringVar(value="PDF")

        shell = ttk.Frame(self, padding=20)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text="Report",
            style="PageTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            shell,
            text=(
                "I report amministrativi sono calcolati dallo storico SQLite "
                "e non includono il codice voucher in chiaro."
            ),
            style="Muted.TLabel",
            wraplength=520,
        ).pack(anchor="w", pady=(2, 16))

        grid = ttk.Frame(shell)
        grid.pack(fill="x")

        ttk.Label(grid, text="Contenuto").grid(
            row=0, column=0, sticky="w", pady=7, padx=(0, 16)
        )
        self.kind_combo = ttk.Combobox(
            grid,
            textvariable=self.kind_var,
            state="readonly",
            values=tuple(label for label, _kind in REPORT_CHOICES),
            width=30,
        )
        self.kind_combo.grid(row=0, column=1, sticky="ew", pady=7)

        ttk.Label(grid, text="Ambito").grid(
            row=1, column=0, sticky="w", pady=7, padx=(0, 16)
        )
        self.scope_combo = ttk.Combobox(
            grid,
            textvariable=self.scope_var,
            state="readonly",
            values=("Controller attivo", "Tutti i controller"),
            width=30,
        )
        self.scope_combo.grid(row=1, column=1, sticky="ew", pady=7)
        if getattr(app, "active_controller_id", None) is None:
            self.scope_var.set("Tutti i controller")

        ttk.Label(grid, text="Formato").grid(
            row=2, column=0, sticky="w", pady=7, padx=(0, 16)
        )
        self.format_combo = ttk.Combobox(
            grid,
            textvariable=self.format_var,
            state="readonly",
            values=("PDF", "CSV"),
            width=14,
        )
        self.format_combo.grid(row=2, column=1, sticky="w", pady=7)

        grid.columnconfigure(1, weight=1)

        ttk.Label(
            shell,
            text=(
                "Nota: “Utilizzi” indica il totale osservato dal controller. "
                "Non viene presentato come ora esatta di utilizzo."
            ),
            style="Muted.TLabel",
            wraplength=520,
        ).pack(anchor="w", pady=(16, 0))

        footer = ttk.Frame(shell)
        footer.pack(fill="x", pady=(22, 0))
        self.cancel_button = ttk.Button(
            footer,
            text="Annulla",
            command=self._close,
            width=12,
        )
        self.cancel_button.pack(side="right")
        self.generate_button = ttk.Button(
            footer,
            text="Genera…",
            command=self._generate,
            style="Accent.TButton",
            width=12,
        )
        self.generate_button.pack(side="right", padx=(0, 8))

        self.update_idletasks()
        width = max(590, self.winfo_reqwidth() + 30)
        height = max(360, self.winfo_reqheight() + 30)
        self.geometry(f"{width}x{height}")
        self.resizable(True, False)

    def _close(self) -> None:
        """Do not destroy Tk widgets while a renderer callback is pending."""

        if self._busy:
            self.bell()
            return
        self.destroy()


    def _set_busy(self, busy: bool) -> None:
        """Prevent duplicate exports while the renderer worker is active."""

        self._busy = bool(busy)
        if busy:
            self.cancel_button.state(["disabled"])
            self.generate_button.state(["disabled"])
            self.kind_combo.state(["disabled"])
            self.scope_combo.state(["disabled"])
            self.format_combo.state(["disabled"])
        else:
            self.cancel_button.state(["!disabled"])
            self.generate_button.state(["!disabled"])
            self.kind_combo.state(["!disabled", "readonly"])
            self.scope_combo.state(["!disabled", "readonly"])
            self.format_combo.state(["!disabled", "readonly"])


    def _generate(self) -> None:
        kind = REPORT_KIND_BY_LABEL[self.kind_var.get()]
        controller_id = None
        if self.scope_var.get() == "Controller attivo":
            controller_id = getattr(self.app, "active_controller_id", None)
            if controller_id is None:
                messagebox.showinfo(
                    "Report",
                    "Nessun controller attivo. Selezionare tutti i controller "
                    "oppure connettersi a un controller.",
                    parent=self,
                )
                return

        generated_at = datetime.now(timezone.utc).isoformat()
        try:
            # SQLite connections stay on their owner Tk thread. Only the
            # renderer runs in the worker below.
            dataset = build_report_dataset(
                self.app.database,
                kind=kind,
                generated_at=generated_at,
                controller_id=controller_id,
            )
        except Exception as exc:
            self.app.logger.error(
                "report_dataset_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Report",
                "Impossibile preparare i dati del report.",
                parent=self,
            )
            return

        extension = ".pdf" if self.format_var.get() == "PDF" else ".csv"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M")
        safe_kind = kind.value.replace("_", "-")
        target = filedialog.asksaveasfilename(
            parent=self,
            title="Salva report",
            defaultextension=extension,
            initialfile=f"Report-{safe_kind}-{timestamp}{extension}",
            filetypes=(
                ("Documento PDF", "*.pdf"),
                ("CSV", "*.csv"),
            )
            if extension == ".pdf"
            else (
                ("CSV", "*.csv"),
                ("Documento PDF", "*.pdf"),
            ),
        )
        if not target:
            return

        output = Path(target)
        installation_name = str(
            self.app.settings.get("structure_name", "") or ""
        )

        def worker():
            if extension == ".pdf":
                render_report_pdf(
                    dataset,
                    output,
                    installation_name=installation_name,
                )
            else:
                render_report_csv(dataset, output)
            return output

        def completed(path: Path) -> None:
            messagebox.showinfo(
                "Report",
                f"Report creato:\n{path}",
                parent=self,
            )
            self._busy = False
            self.destroy()

        def failed(exc: Exception) -> None:
            self.app.logger.error(
                "report_render_failed type=%s",
                type(exc).__name__,
            )
            messagebox.showerror(
                "Report",
                "Creazione del report non riuscita.",
                parent=self,
            )

        self.app._run_background_task(
            "Generazione report…",
            worker,
            completed,
            failed,
            busy_scope=self._set_busy,
        )
