"""Report PDF preview with save/print choices, separate from voucher print audit."""
from pathlib import Path
import shutil
from tkinter import filedialog, messagebox, ttk
from .pdf_preview import PdfPreview


class ReportPreview(PdfPreview):
    def __init__(self, app, pdf_path, temporary_directory):
        self._closing = False
        self._temporary_directory = temporary_directory
        super().__init__(app, pdf_path, [], None, app.settings)
        self.title("Anteprima report - Salva o stampa")
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build_ui(self):
        super()._build_ui()
        # Replace voucher-specific instructions. Reports must never write to
        # voucher print history, including when physically printed.
        bottom = self.print_button.master
        for widget in bottom.grid_slaves(row=1):
            widget.destroy()
        ttk.Label(bottom, text="Il report consulta lo storico locale. La stampa del report non modifica lo stato dei voucher.", wraplength=680).grid(row=1, column=0, columnspan=6, sticky="w", pady=8)
        ttk.Button(bottom, text="Salva PDF…", command=self._save).grid(row=2, column=0, pady=8)
        ttk.Button(bottom, text="Chiudi", command=self.destroy).grid(row=2, column=4, pady=8)

    def _save(self):
        target = filedialog.asksaveasfilename(parent=self, title="Salva report", initialfile=self.pdf_path.name, defaultextension=".pdf", filetypes=(("Documento PDF", "*.pdf"),))
        if target:
            try:
                shutil.copyfile(self.pdf_path, Path(target))
            except OSError:
                messagebox.showerror("Report", "Impossibile salvare il report nella posizione selezionata.", parent=self)

    def print_document(self):
        if self._printing:
            return
        printer = self.printer_var.get().strip()
        try:
            copies = int(self.copies_var.get())
        except (ValueError, TypeError):
            copies = 0
        if not printer or not 1 <= copies <= 99:
            messagebox.showerror("Stampa", "Seleziona una stampante e da 1 a 99 copie.", parent=self)
            return
        self._printing = True
        self.print_button.state(["disabled"])

        def finish():
            self._printing = False
            self.print_button.state(["!disabled"])

        def completed(_):
            finish()
            messagebox.showinfo("Stampa report", "Documento inviato alla stampante.", parent=self)

        def failed(_):
            finish()
            messagebox.showerror("Stampa report", "Invio interrotto. Verifica la coda della stampante prima di riprovare.", parent=self)

        try:
            started = self.app._run_background_task("Stampa report…", lambda: self._print_windows(printer, copies), completed, failed)
        except Exception:
            finish()
            raise
        if not started:
            finish()

    def _resize(self, event):
        if not self._closing:
            super()._resize(event)

    def destroy(self):
        if getattr(self, "_printing", False):
            self.bell()
            return
        self._closing = True
        # Windows may keep the file open during an asynchronous raster. Wait
        # for the existing preview worker before deleting its temporary PDF.
        if self._render_results is not None:
            self.after(50, self.destroy)
            return
        super().destroy()
        self._temporary_directory.cleanup()
