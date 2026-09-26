"""Embedded final-PDF preview and Windows printing for Revision 3.

The preview always renders the PDF file already written to disk. It does not
rebuild a second Tk representation of the voucher layout; therefore the user
sees the definitive document that will be sent to the printer.

Revision 3.3 makes the preview layout explicitly operator-first: the complete
A4 page must remain visible while printer controls remain permanently
accessible. Only the PDF viewport is allowed to grow/shrink with the window.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from queue import Empty
import tkinter as tk
from uuid import uuid4
from tkinter import messagebox, ttk

import pypdfium2 as pdfium
from PIL import Image, ImageTk, ImageWin

from .background_tasks import start_background_task

try:
    import win32con
    import win32print
    import win32ui
except ImportError:  # Allows source inspection/tests on non-Windows hosts.
    win32con = win32print = win32ui = None


class PdfPreview(tk.Toplevel):
    """End-user viewer that always fits one complete A4 page in its viewport."""

    def __init__(
        self,
        parent,
        pdf_path: Path,
        codes: list[str],
        history,
        settings: dict,
        on_print=None,
        on_audit=None,
        on_submitted=None,
    ):
        super().__init__(parent)
        self.app = parent
        self.pdf_path = Path(pdf_path)
        self.codes = list(codes)
        self.history = history
        self.settings = settings
        self.on_print = on_print
        # Optional application-level audit (SQLite in 5.0). It runs on the Tk
        # thread after the crash-safe HMAC audit succeeds, so the SQLite
        # connection is never shared with the print worker.
        self.on_audit = on_audit
        # Called only after the Windows submission returned successfully.
        # This is distinct from on_print, which also refreshes UI around audit
        # recovery and ambiguous prepared jobs.
        self.on_submitted = on_submitted
        self.document = None
        self.page_index = 0
        self.photo = None
        self._render_after = None
        self._render_results = None
        self._render_poll_after = None
        self._render_generation = 0
        self._render_active_generation = 0
        self._printing = False
        self._pending_print_audit = None
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
        self.register_print_button = ttk.Button(
            bottom,
            text="REGISTRA STAMPA",
            command=self.register_print_audit,
        )
        self.register_print_button.grid(
            row=0,
            column=5,
            padx=(8, 0),
        )
        self.register_print_button.grid_remove()

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
        """Rasterise the definitive PDF off Tk and fit the entire A4 page."""

        self._render_after = None
        if self.document is None or not len(self.document):
            return

        self._render_generation += 1
        generation = self._render_generation

        # If a previous raster is still running, do not fan out threads during
        # resize. Its completion notices the newer generation and immediately
        # schedules one render for the latest page/viewport instead.
        if self._render_results is not None:
            return

        page_index = self.page_index
        page_count = len(self.document)
        pdf_path = self.pdf_path
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        margin = 20
        available_w = max(1, canvas_w - margin * 2)
        available_h = max(1, canvas_h - margin * 2)

        def worker():
            document = pdfium.PdfDocument(str(pdf_path))
            try:
                if page_index >= len(document):
                    raise RuntimeError("Pagina PDF non disponibile")
                page = document[page_index]
                try:
                    image = page.render(scale=2.0).to_pil().convert("RGB")
                finally:
                    page.close()
            finally:
                document.close()

            image.thumbnail(
                (available_w, available_h),
                Image.Resampling.LANCZOS,
            )
            return (
                generation,
                page_index,
                page_count,
                canvas_w,
                canvas_h,
                image,
            )

        self._render_active_generation = generation
        self._render_results = start_background_task(worker)
        self._render_poll_after = self.after(20, self._poll_render)

    def _poll_render(self) -> None:
        """Apply worker-owned raster results only from the Tk thread."""

        self._render_poll_after = None
        results = self._render_results
        if results is None:
            return

        try:
            result = results.get_nowait()
        except Empty:
            self._render_poll_after = self.after(20, self._poll_render)
            return

        self._render_results = None
        active_generation = self._render_active_generation
        self._render_active_generation = 0

        if active_generation != self._render_generation:
            self._render_after = self.after(0, self.render_page)
            return

        if result.error is not None:
            self.page_var.set("Anteprima non disponibile")
            return

        (
            generation,
            page_index,
            page_count,
            canvas_w,
            canvas_h,
            image,
        ) = result.value

        if generation != self._render_generation:
            self._render_after = self.after(0, self.render_page)
            return

        self.photo = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        self.canvas.create_image(
            canvas_w / 2,
            canvas_h / 2,
            image=self.photo,
            anchor="center",
        )
        self.page_var.set(
            f"Pagina {page_index + 1} / {page_count}"
        )

    def previous(self):
        if self.page_index > 0:
            self.page_index -= 1
            self.render_page()

    def next(self):
        if self.page_index + 1 < len(self.document):
            self.page_index += 1
            self.render_page()

    def print_document(self):
        """Submit/rasterise on the shared worker without blocking Tk."""

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
        self.register_print_button.state(["disabled"])

        pdf_path = self.pdf_path
        codes = list(self.codes)
        history = self.history
        settings = dict(self.settings)
        application_audit = getattr(self, "on_audit", None)

        def worker():
            history.assert_no_pending_print_audit()
            pending = {
                "copies": copies,
                "audit_id": uuid4().hex,
                "submitted_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }

            # Persist the exact audit descriptor before entering the Windows
            # printing API. If the process stops during submission, startup can
            # distinguish an ambiguous "prepared" job from one known to have
            # been submitted instead of silently losing the lifecycle boundary.
            history.prepare_print_audit(
                codes,
                pdf_path,
                copies,
                settings,
                audit_id=pending["audit_id"],
                submitted_at=pending["submitted_at"],
            )

            self._print_windows(printer, copies)

            try:
                history.mark_print_submitted(pending["audit_id"])
                record_kwargs = {}
                if application_audit is not None:
                    # Keep the durable descriptor until SQLite commits the
                    # same audit_id on the Tk thread.
                    record_kwargs["clear_pending"] = False
                history.record_print(
                    codes,
                    pdf_path,
                    copies,
                    settings,
                    audit_id=pending["audit_id"],
                    submitted_at=pending["submitted_at"],
                    **record_kwargs,
                )
            except Exception as exc:
                # Physical submission has already returned successfully. Keep
                # the durable descriptor and expose audit-only recovery; never
                # ask the operator to resend the document.
                return pending, exc
            return pending, None

        def finish_controls() -> bool:
            self._printing = False
            try:
                alive = bool(self.winfo_exists())
            except tk.TclError:
                alive = False
            if alive:
                self.print_button.state(["!disabled"])
                self.register_print_button.state(["!disabled"])
            return alive

        def completed(result) -> None:
            pending, audit_error = result
            if not finish_controls():
                return

            on_submitted = getattr(self, "on_submitted", None)
            if on_submitted:
                on_submitted()

            if audit_error is None:
                try:
                    if application_audit:
                        application_audit(
                            dict(pending),
                            list(codes),
                            Path(pdf_path),
                        )
                        history.finalize_pending_print_audit(
                            str(pending["audit_id"])
                        )
                except Exception as exc:
                    # The physical print and HMAC history are already durable.
                    # Keep an in-memory audit-only retry path; never resend the
                    # document because SQLite persistence failed.
                    audit_error = exc

            if audit_error is not None:
                self._pending_print_audit = pending
                self.register_print_button.grid()
                if self.on_print:
                    self.on_print()
                messagebox.showwarning(
                    "Stampa inviata - archivio non aggiornato",
                    f"Il documento è stato inviato a {printer}, ma una parte "
                    "dell'archivio locale non è stata aggiornata.\n\n"
                    f"{audit_error}\n\n"
                    "Non ristampare il voucher. Usare REGISTRA STAMPA per "
                    "ritentare soltanto la registrazione.",
                    parent=self,
                )
                return

            self._pending_print_audit = None
            self.register_print_button.grid_remove()
            if self.on_print:
                self.on_print()
            messagebox.showinfo(
                "Stampa",
                f"Documento inviato a {printer}.",
                parent=self,
            )

        def failed(exc: Exception) -> None:
            if not finish_controls():
                return
            try:
                pending_state = history.pending_print_state()
            except Exception:
                pending_state = ""

            if pending_state == "prepared":
                if self.on_print:
                    self.on_print()
                messagebox.showwarning(
                    "Esito stampa da verificare",
                    "L'invio alla stampante si è interrotto dopo aver "
                    "preparato la registrazione di sicurezza. Non è possibile "
                    "stabilire automaticamente se Windows abbia ricevuto il "
                    "documento.\n\n"
                    f"{exc}\n\n"
                    "Non ristampare finché la stampa pendente non viene "
                    "risolta da Impostazioni > Recupera stampa pendente.",
                    parent=self,
                )
                return

            messagebox.showerror(
                "Stampa",
                "Impossibile inviare il documento alla stampante.\n\n"
                f"{exc}",
                parent=self,
            )

        started = self.app._run_background_task(
            "Rasterizzazione e invio alla stampante…",
            worker,
            completed,
            failed,
        )
        if not started:
            finish_controls()

    def register_print_audit(self) -> None:
        """Retry only the audit write for an already-submitted print job."""

        pending = self._pending_print_audit
        if not pending or self._printing:
            return

        self._printing = True
        self.print_button.state(["disabled"])
        self.register_print_button.state(["disabled"])

        history = self.history
        codes = list(self.codes)
        pdf_path = self.pdf_path
        settings = dict(self.settings)
        stable_pending = dict(pending)
        application_audit = getattr(self, "on_audit", None)

        def worker():
            record_kwargs = {}
            if application_audit is not None:
                record_kwargs["clear_pending"] = False
            history.record_print(
                codes,
                pdf_path,
                int(stable_pending["copies"]),
                settings,
                audit_id=str(stable_pending["audit_id"]),
                submitted_at=str(stable_pending["submitted_at"]),
                **record_kwargs,
            )

        def finish_controls() -> bool:
            self._printing = False
            try:
                alive = bool(self.winfo_exists())
            except tk.TclError:
                alive = False
            if alive:
                self.print_button.state(["!disabled"])
                self.register_print_button.state(["!disabled"])
            return alive

        def completed(_result) -> None:
            if not finish_controls():
                return
            try:
                if application_audit:
                    application_audit(
                        dict(stable_pending),
                        list(codes),
                        Path(pdf_path),
                    )
                    history.finalize_pending_print_audit(
                        str(stable_pending["audit_id"])
                    )
            except Exception as exc:
                messagebox.showerror(
                    "Registrazione stampa",
                    "Lo storico HMAC è disponibile, ma l'archivio SQLite "
                    "non è stato aggiornato. Non ristampare il voucher.\n\n"
                    f"{exc}",
                    parent=self,
                )
                return

            self._pending_print_audit = None
            self.register_print_button.grid_remove()
            if self.on_print:
                self.on_print()
            messagebox.showinfo(
                "Registrazione stampa",
                "La stampa già inviata è stata registrata correttamente "
                "nello storico.",
                parent=self,
            )

        def failed(exc: Exception) -> None:
            if not finish_controls():
                return
            messagebox.showerror(
                "Registrazione stampa",
                "Lo storico non è stato aggiornato. La stampa fisica "
                "risulta già inviata: non ristampare il voucher.\n\n"
                f"{exc}",
                parent=self,
            )

        started = self.app._run_background_task(
            "Registrazione stampa…",
            worker,
            completed,
            failed,
        )
        if not started:
            finish_controls()

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

            # Printing runs on a worker. Open a worker-owned PDFium
            # document instead of sharing the preview document with Tk.
            document = pdfium.PdfDocument(str(self.pdf_path))
            try:
                for page_index in range(len(document)):
                    # Render each PDF page once, then reuse the raster for all
                    # requested copies. Voucher sheets are independent pages.
                    page = document[page_index]
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
            finally:
                document.close()

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
        if self._render_poll_after is not None:
            try:
                self.after_cancel(self._render_poll_after)
            except tk.TclError:
                pass
            self._render_poll_after = None
        self._render_generation += 1
        self._render_active_generation = 0
        self._render_results = None
        document = self.document
        self.document = None
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
        super().destroy()
