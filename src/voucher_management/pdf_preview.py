"""Embedded final-PDF preview and Windows printing for Revision 3.

The preview always renders the PDF file already written to disk. It does not
rebuild a second Tk representation of the voucher layout; therefore the user
sees the definitive document that will be sent to the printer.

Revision 3.3 makes the preview layout explicitly operator-first: the complete
A4 page must remain visible while printer controls remain permanently
accessible. Only the PDF viewport is allowed to grow/shrink with the window.
"""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import pypdfium2 as pdfium
from PIL import Image, ImageTk, ImageWin

try:
    import win32con
    import win32print
    import win32ui
except ImportError:  # Allows source inspection/tests on non-Windows hosts.
    win32con = win32print = win32ui = None


class PdfPreview(tk.Toplevel):
    """End-user viewer that always fits one complete A4 page in its viewport."""

    def __init__(self, parent, pdf_path: Path, codes: list[str], history, settings: dict, on_print=None):
        super().__init__(parent)
        self.pdf_path = Path(pdf_path)
        self.codes = list(codes)
        self.history = history
        self.settings = settings
        self.on_print = on_print
        self.document = None
        self.page_index = 0
        self.photo = None
        self._render_after = None
        self._printing = False
        try:
            self.document = pdfium.PdfDocument(str(self.pdf_path))
        except Exception as exc:
            # The Toplevel already exists at this point. Destroy it immediately
            # so a PDF load failure cannot leave an empty orphan window.
            super().destroy()
            raise RuntimeError("Impossibile caricare il documento PDF") from exc

        self.title(f"Anteprima di stampa - {self.pdf_path.name}")
        # A sensible fallback is kept for environments where Windows refuses
        # the zoomed state. On normal Windows desktops the window is maximised
        # after widgets exist, giving the A4 viewport the largest safe area.
        self.geometry("1000x800")
        self.minsize(760, 620)

        self.page_var = tk.StringVar()
        self.printer_var = tk.StringVar()
        self.copies_var = tk.IntVar(value=1)
        self._build_ui()
        self._load_printers()
        self.bind("<Configure>", self._resize)
        self.after_idle(self._maximize_window)
        self.after(120, self.render_page)

    def _build_ui(self):
        """Build three independent rows: navigation, viewport and print bar.

        The navigation and print rows use grid without expansion. The canvas is
        the only row with weight=1, so a smaller window can reduce only the PDF
        viewport; it cannot push the printer selector or STAMPA button outside
        the visible client area.
        """
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        bar = ttk.Frame(self, padding=8)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="◀", width=4, command=self.previous).pack(side="left")
        ttk.Label(bar, textvariable=self.page_var, width=16, anchor="center").pack(side="left", padx=6)
        ttk.Button(bar, text="▶", width=4, command=self.next).pack(side="left")
        ttk.Label(bar, text="Anteprima definitiva del foglio A4", foreground="#555").pack(side="left", padx=16)

        # No scrollbars by design. render_page() always calculates a single
        # proportional scale that fits both page width and height in the canvas.
        self.canvas = tk.Canvas(self, background="#777", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        bottom = ttk.Frame(self, padding=8)
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.grid_columnconfigure(1, weight=1)
        ttk.Label(bottom, text="Stampante:").grid(row=0, column=0, sticky="w")
        self.printers = ttk.Combobox(bottom, textvariable=self.printer_var, state="readonly", width=42)
        self.printers.grid(row=0, column=1, sticky="ew", padx=(5, 14))
        ttk.Label(bottom, text="Copie documento:").grid(row=0, column=2, sticky="e")
        ttk.Spinbox(bottom, from_=1, to=99, textvariable=self.copies_var, width=5).grid(row=0, column=3, padx=5)
        self.print_button = ttk.Button(
            bottom,
            text="STAMPA",
            command=self.print_document,
        )
        self.print_button.grid(row=0, column=4, padx=(14, 5))

    def _maximize_window(self):
        """Maximise on Windows without entering borderless/full-screen mode."""
        try:
            self.state("zoomed")
        except tk.TclError:
            # Some Tk/window-manager combinations do not implement 'zoomed'.
            # The fallback geometry still preserves the fixed command bar and
            # fit-page invariant, so usability does not depend on maximisation.
            pass

    def _load_printers(self):
        if win32print is None:
            return
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        names = [p[2] for p in win32print.EnumPrinters(flags)]
        self.printers["values"] = names
        try:
            default = win32print.GetDefaultPrinter()
        except Exception:
            default = ""
        self.printer_var.set(default if default in names else (names[0] if names else ""))

    def _resize(self, _event=None):
        """Debounce expensive PDFium rendering during interactive resize."""
        if self._render_after is not None:
            try:
                self.after_cancel(self._render_after)
            except tk.TclError:
                pass
        self._render_after = self.after(120, self.render_page)

    def render_page(self):
        """Render the definitive PDF and fit the *entire* page in the canvas.

        Image.thumbnail() preserves aspect ratio and constrains both dimensions,
        so neither the top/bottom nor left/right edge of the A4 sheet can be
        clipped by normal window resizing. A small fixed margin keeps the white
        page visually separated from the grey preview background.
        """
        self._render_after = None
        if self.document is None or not len(self.document):
            return
        page = self.document[self.page_index]
        try:
            image = page.render(scale=2.0).to_pil().convert("RGB")
        finally:
            page.close()
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        margin = 20
        available_w = max(1, canvas_w - margin * 2)
        available_h = max(1, canvas_h - margin * 2)
        image.thumbnail((available_w, available_h), Image.Resampling.LANCZOS)

        self.photo = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        self.canvas.create_image(canvas_w / 2, canvas_h / 2, image=self.photo, anchor="center")
        self.page_var.set(f"Pagina {self.page_index + 1} / {len(self.document)}")

    def previous(self):
        if self.page_index > 0:
            self.page_index -= 1
            self.render_page()

    def next(self):
        if self.page_index + 1 < len(self.document):
            self.page_index += 1
            self.render_page()

    def print_document(self):
        if self._printing:
            return
        printer = self.printer_var.get().strip()
        try:
            copies = int(self.copies_var.get())
        except Exception:
            copies = 0
        if not printer or not 1 <= copies <= 99:
            messagebox.showerror(
                "Stampa",
                "Selezionare una stampante e un numero di copie valido.",
                parent=self,
            )
            return

        self._printing = True
        self.print_button.state(["disabled"])
        try:
            try:
                self._print_windows(printer, copies)
            except Exception as exc:
                messagebox.showerror(
                    "Stampa",
                    f"Impossibile inviare il documento alla stampante.\n\n{exc}",
                    parent=self,
                )
                return

            # From this point the print job has already been submitted. Audit
            # failure must never be reported as a print failure, otherwise an
            # operator may submit an accidental duplicate.
            try:
                self.history.record_print(
                    self.codes,
                    self.pdf_path,
                    copies,
                    self.settings,
                )
            except Exception as exc:
                if self.on_print:
                    self.on_print()
                messagebox.showwarning(
                    "Stampa inviata - storico non aggiornato",
                    f"Il documento è stato inviato a {printer}, ma lo storico "
                    f"locale non è stato aggiornato.\n\n{exc}\n\n"
                    "Non ristampare automaticamente il voucher.",
                    parent=self,
                )
                return

            if self.on_print:
                self.on_print()
            messagebox.showinfo(
                "Stampa",
                f"Documento inviato a {printer}.",
                parent=self,
            )
        finally:
            self._printing = False
            if self.winfo_exists():
                self.print_button.state(["!disabled"])

    def _print_windows(self, printer: str, copies: int):
        """Rasterise with PDFium and send pages directly to a Windows printer DC.

        This deliberately avoids ShellExecute/Adobe. A4 aspect ratio is kept
        inside the printer's physical printable area, so cut geometry is never
        stretched independently in X/Y.
        """
        if win32ui is None:
            raise RuntimeError("Supporto stampa Windows non disponibile")
        dc = win32ui.CreateDC()
        document_started = False
        try:
            dc.CreatePrinterDC(printer)
            printable_w = dc.GetDeviceCaps(win32con.HORZRES)
            printable_h = dc.GetDeviceCaps(win32con.VERTRES)
            dc.StartDoc(self.pdf_path.name)
            document_started = True

            if self.document is None:
                raise RuntimeError("Documento PDF non disponibile")

            for page_index in range(len(self.document)):
                # Render each PDF page once, then reuse the raster for all
                # requested copies. Voucher sheets are independent pages, so
                # page-major output avoids repeated PDFium work without
                # changing page contents or print scaling.
                page = self.document[page_index]
                try:
                    image = (
                        page.render(scale=300 / 72)
                        .to_pil()
                        .convert("RGB")
                    )
                finally:
                    page.close()

                scale = min(
                    printable_w / image.width,
                    printable_h / image.height,
                )
                w = max(1, int(image.width * scale))
                h = max(1, int(image.height * scale))
                x = (printable_w - w) // 2
                y = (printable_h - h) // 2
                dib = ImageWin.Dib(image)

                for _copy in range(copies):
                    dc.StartPage()
                    dib.draw(
                        dc.GetHandleOutput(),
                        (x, y, x + w, y + h),
                    )
                    dc.EndPage()

            dc.EndDoc()
            document_started = False
        except Exception:
            if document_started:
                try:
                    dc.AbortDoc()
                except Exception:
                    pass
            raise
        finally:
            dc.DeleteDC()

    def destroy(self):
        """Cancel pending work and release the PDFium document deterministically."""

        if self._render_after is not None:
            try:
                self.after_cancel(self._render_after)
            except tk.TclError:
                pass
            self._render_after = None
        document = self.document
        self.document = None
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
        super().destroy()
