"""Tk dialog for exporting printable/privacy-safe 5.0 reports."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from .report_guide import HISTORY_NOTICE, REPORT_GUIDE
from tkinter import filedialog, messagebox, ttk

from .report_render import render_report_csv, render_report_pdf
from .report_temp import create_report_temporary_directory
from .reporting import ReportKind, build_report_dataset_from_path
from .history_sqlite_reconciliation import HistorySqliteReconciliationError


REPORT_CHOICES = (
    ("Riepilogo storico", ReportKind.SUMMARY),
    ("Creati con questo software", ReportKind.GENERATED),
    ("Creati con questo software - nessun utilizzo rilevato", ReportKind.GENERATED_UNUSED),
    ("Utilizzati almeno una volta", ReportKind.USED),
    ("Scaduti", ReportKind.EXPIRED),
    ("Stampati", ReportKind.PRINTED),
    ("Stampati - nessun utilizzo rilevato", ReportKind.PRINTED_UNUSED),
    ("Senza stampe registrate", ReportKind.NEVER_PRINTED),
    ("Nominali", ReportKind.NOMINAL),
    ("Non nominali", ReportKind.NON_NOMINAL),
    ("Non classificati", ReportKind.UNCLASSIFIED),
    ("Uso non determinabile", ReportKind.USAGE_UNKNOWN),
    ("Origine creazione non determinabile", ReportKind.ORIGIN_UNKNOWN),
    ("Nominalità rimossa per privacy", ReportKind.NOMINALITY_REDACTED),
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
        self.scope_var = tk.StringVar(value="Tutto lo storico locale")
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
                HISTORY_NOTICE + " I codici voucher non sono esportati in chiaro. "
                "Il Riepilogo storico contiene solo aggregati; i report di dettaglio "
                "possono contenere descrizioni UniFi, destinatari locali e "
                "account Windows degli operatori."
            ),
            style="Muted.TLabel",
            wraplength=520,
        ).pack(anchor="w", pady=(2, 16))

        self.help_var = tk.StringVar(value=REPORT_GUIDE[ReportKind.SUMMARY][1])
        ttk.Button(shell, text="Aiutami a scegliere", command=self._guide).pack(anchor="w", pady=(0, 8))
        ttk.Label(shell, textvariable=self.help_var, wraplength=560, style="Muted.TLabel").pack(anchor="w", pady=(0, 12))

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
            width=56,
        )
        self.kind_combo.grid(row=0, column=1, sticky="ew", pady=7)
        self.kind_combo.bind("<<ComboboxSelected>>", lambda _: self.help_var.set(REPORT_GUIDE[REPORT_KIND_BY_LABEL[self.kind_var.get()]][1]))

        ttk.Label(grid, text="Ambito").grid(
            row=1, column=0, sticky="w", pady=7, padx=(0, 16)
        )
        self.scope_combo = ttk.Combobox(
            grid,
            textvariable=self.scope_var,
            state="readonly",
            values=("Tutto lo storico locale", "Controller attivo"),
            width=30,
        )
        self.scope_combo.grid(row=1, column=1, sticky="ew", pady=7)

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
                "Nota: “Nessun utilizzo rilevato” descrive solo ciò che Voucher "
                "Management ha visto fino all'ultima osservazione controller "
                "riportata nel file. Se l'evidenza manca, il voucher resta in "
                "“Uso non determinabile”. La nominalità è una classificazione "
                "locale esplicita."
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
            text="Genera anteprima / CSV…",
            command=self._generate,
            style="Accent.TButton",
            width=26,
        )
        self.generate_button.pack(side="right", padx=(0, 8))

        self.update_idletasks()
        width = max(590, self.winfo_reqwidth() + 30)
        height = max(360, self.winfo_reqheight() + 30)
        self.geometry(f"{width}x{height}")
        self.resizable(True, False)

    def _guide(self):
        guide = tk.Toplevel(self)
        guide.title("Che cosa vuoi sapere?")
        guide.transient(self)
        guide.grab_set()
        shell = ttk.Frame(guide, padding=16)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="Scegli un'esigenza. Il report si aprirà in anteprima prima di salvarlo o stamparlo.", wraplength=600).pack(anchor="w", pady=8)
        tabs = ttk.Notebook(shell)
        tabs.pack(fill="both", expand=True)
        normal, quality = ttk.Frame(tabs, padding=12), ttk.Frame(tabs, padding=12)
        tabs.add(normal, text="Report operativi")
        tabs.add(quality, text="Verifica dei dati")
        def select(kind):
            label = next(label for label, value in REPORT_CHOICES if value == kind)
            self.kind_var.set(label)
            self.help_var.set(REPORT_GUIDE[kind][1])
            close()
        def close():
            guide.destroy()
            self.grab_set()
        for kind, (question, _) in REPORT_GUIDE.items():
            parent = quality if question.startswith("Verifica dati:") else normal
            ttk.Button(parent, text=question, command=lambda k=kind: select(k)).pack(fill="x", pady=3)
        ttk.Button(shell, text="Chiudi", command=close).pack(anchor="e", pady=8)
        guide.protocol("WM_DELETE_WINDOW", close)

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
                    "Nessun controller attivo. Usare “Tutto lo storico locale” "
                    "oppure connettersi a un controller.",
                    parent=self,
                )
                return

        generated_at = datetime.now(timezone.utc).isoformat()
        extension = ".pdf" if self.format_var.get() == "PDF" else ".csv"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M")
        safe_kind = kind.value.replace("_", "-")
        temporary = None
        if extension == ".pdf":
            temporary = create_report_temporary_directory()
            output = Path(temporary.name) / f"Report-{safe_kind}-{timestamp}.pdf"
        else:
            target = filedialog.asksaveasfilename(parent=self, title="Salva CSV", defaultextension=".csv", initialfile=f"Report-{safe_kind}-{timestamp}.csv", filetypes=(("CSV", "*.csv"),))
            if not target:
                return
            output = Path(target)

        installation_name = str(
            self.app.settings.get("structure_name", "") or ""
        )
        database_path = Path(self.app.paths.database)

        def worker():
            dataset = build_report_dataset_from_path(
                database_path,
                kind=kind,
                generated_at=generated_at,
                controller_id=controller_id,
            )
            if extension == ".pdf":
                render_report_pdf(
                    dataset,
                    output,
                    installation_name=installation_name,
                )
            else:
                render_report_csv(dataset, output)
            return output, dataset

        def completed(result) -> None:
            path, dataset = result
            self._busy = False
            if temporary is not None:
                try:
                    from .report_preview import ReportPreview
                    ReportPreview(
                        self.app,
                        path,
                        temporary,
                        result_count=dataset.totals.vouchers,
                    )
                except Exception:
                    temporary.cleanup()
                    messagebox.showerror(
                        "Report",
                        "Impossibile aprire l'anteprima. Riprova la generazione.",
                        parent=self,
                    )
                    return
            else:
                count = dataset.totals.vouchers
                detail = (
                    "Nessun voucher soddisfa i criteri; il CSV contiene comunque "
                    "ambito, copertura dati e riepilogo."
                    if count == 0
                    else f"{count} voucher nel report."
                )
                messagebox.showinfo(
                    "Report",
                    f"CSV salvato:\n{path}\n\n{detail}",
                    parent=self,
                )
            self.destroy()

        def failed(exc: Exception) -> None:
            if temporary is not None:
                temporary.cleanup()
            self.app.logger.error(
                "report_generation_failed type=%s",
                type(exc).__name__,
            )
            detail = (
                str(exc)
                if isinstance(exc, HistorySqliteReconciliationError)
                else "Creazione del report non riuscita."
            )
            messagebox.showerror(
                "Report",
                detail,
                parent=self,
            )

        started = self.app._run_background_task(
            "Generazione report…",
            worker,
            completed,
            failed,
            busy_scope=self._set_busy,
        )

        if not started and temporary is not None:
            temporary.cleanup()
