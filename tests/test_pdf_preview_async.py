from pathlib import Path
from queue import Queue
from types import SimpleNamespace

from PIL import Image

from voucher_management import pdf_preview
from voucher_management.background_tasks import BackgroundResult
from voucher_management.pdf_preview import PdfPreview


class _Canvas:
    def __init__(self, width=800, height=600):
        self.width = width
        self.height = height
        self.deleted = []
        self.created = []

    def winfo_width(self):
        return self.width

    def winfo_height(self):
        return self.height

    def delete(self, target):
        self.deleted.append(target)

    def create_image(self, *args, **kwargs):
        self.created.append((args, kwargs))


class _Var:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class _PreviewDocument:
    def __len__(self):
        return 1


def test_render_page_defers_pdfium_rasterization_to_worker(monkeypatch):
    captured = {}
    scheduled = []
    opened = []

    class Page:
        def render(self, *, scale):
            assert scale == 2.0
            return SimpleNamespace(
                to_pil=lambda: Image.new("RGB", (2000, 3000), "white")
            )

        def close(self):
            captured["page_closed"] = True

    class WorkerDocument:
        def __init__(self, path):
            opened.append(path)

        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return Page()

        def close(self):
            captured["document_closed"] = True

    result_queue = object()

    def start(worker):
        captured["worker"] = worker
        return result_queue

    fake = SimpleNamespace(
        document=_PreviewDocument(),
        page_index=0,
        pdf_path=Path("synthetic.pdf"),
        canvas=_Canvas(),
        _render_after="debounce",
        _render_results=None,
        _render_poll_after=None,
        _render_generation=0,
        _render_active_generation=0,
        _poll_render=lambda: None,
        after=lambda delay, callback: (
            scheduled.append((delay, callback)) or "poll-token"
        ),
    )
    monkeypatch.setattr(pdf_preview, "start_background_task", start)
    monkeypatch.setattr(pdf_preview.pdfium, "PdfDocument", WorkerDocument)

    PdfPreview.render_page(fake)

    assert opened == []
    assert fake._render_results is result_queue
    assert fake._render_generation == 1
    assert scheduled and scheduled[0][0] == 20

    result = captured["worker"]()

    assert opened == ["synthetic.pdf"]
    assert captured["page_closed"] is True
    assert captured["document_closed"] is True
    assert result[:5] == (1, 0, 1, 800, 600)
    assert result[5].width <= 760
    assert result[5].height <= 560


def test_poll_render_applies_photo_only_on_tk_side(monkeypatch):
    image = Image.new("RGB", (200, 300), "white")
    results = Queue()
    results.put(
        BackgroundResult(
            value=(1, 0, 1, 800, 600, image)
        )
    )
    canvas = _Canvas()
    page_var = _Var()
    fake = SimpleNamespace(
        _render_poll_after="poll-token",
        _render_results=results,
        _render_generation=1,
        _render_active_generation=1,
        _render_after=None,
        page_var=page_var,
        canvas=canvas,
        photo=None,
        after=lambda delay, callback: "unused",
    )
    monkeypatch.setattr(
        pdf_preview.ImageTk,
        "PhotoImage",
        lambda current: ("photo", current),
    )

    PdfPreview._poll_render(fake)

    assert fake._render_results is None
    assert fake.photo == ("photo", image)
    assert canvas.deleted == ["all"]
    assert canvas.created[0][0] == (400.0, 300.0)
    assert page_var.value == "Pagina 1 / 1"


def test_stale_preview_result_is_discarded_and_latest_render_is_scheduled(
    monkeypatch,
):
    results = Queue()
    results.put(
        BackgroundResult(
            value=(1, 0, 1, 800, 600, Image.new("RGB", (10, 10)))
        )
    )
    scheduled = []
    fake = SimpleNamespace(
        _render_poll_after="poll-token",
        _render_results=results,
        _render_generation=2,
        _render_active_generation=1,
        _render_after=None,
        page_var=_Var(),
        canvas=_Canvas(),
        photo=None,
        render_page=lambda: None,
        after=lambda delay, callback: (
            scheduled.append((delay, callback)) or "latest-token"
        ),
    )
    monkeypatch.setattr(
        pdf_preview.ImageTk,
        "PhotoImage",
        lambda current: (_ for _ in ()).throw(
            AssertionError("stale image must not reach Tk")
        ),
    )

    PdfPreview._poll_render(fake)

    assert fake._render_results is None
    assert fake._render_after == "latest-token"
    assert scheduled and scheduled[0][0] == 0


def test_preview_worker_error_does_not_raise_into_tk_callback():
    results = Queue()
    results.put(BackgroundResult(error=RuntimeError("synthetic")))
    page_var = _Var()
    fake = SimpleNamespace(
        _render_poll_after="poll-token",
        _render_results=results,
        _render_generation=1,
        _render_active_generation=1,
        _render_after=None,
        page_var=page_var,
    )

    PdfPreview._poll_render(fake)

    assert fake._render_results is None
    assert page_var.value == "Anteprima non disponibile"


def test_stale_preview_worker_error_is_ignored_for_newer_generation():
    results = Queue()
    results.put(BackgroundResult(error=RuntimeError("stale")))
    scheduled = []
    page_var = _Var()
    fake = SimpleNamespace(
        _render_poll_after="poll-token",
        _render_results=results,
        _render_generation=2,
        _render_active_generation=1,
        _render_after=None,
        page_var=page_var,
        render_page=lambda: None,
        after=lambda delay, callback: (
            scheduled.append((delay, callback)) or "latest-token"
        ),
    )

    PdfPreview._poll_render(fake)

    assert page_var.value == ""
    assert fake._render_after == "latest-token"
    assert scheduled and scheduled[0][0] == 0
