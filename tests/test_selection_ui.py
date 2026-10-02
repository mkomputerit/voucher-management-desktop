from types import SimpleNamespace

import pytest

from voucher_management.app import VoucherApp
from voucher_management.modern_app import ModernVoucherApp


class FakeVar:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class FakeTree:
    def __init__(self, rows):
        self.rows = {key: list(values) for key, values in rows.items()}
        self.writes = []

    def identify_region(self, x, y):
        return "cell"

    def identify_column(self, x):
        return "#1"

    def identify_row(self, y):
        return "row-1"

    def item(self, iid, option=None, **kwargs):
        if option == "values":
            return tuple(self.rows[iid])
        if "values" in kwargs:
            self.rows[iid] = list(kwargs["values"])
            self.writes.append(iid)
            return None
        raise AssertionError("unexpected Treeview.item call")


def _selection_app(tree, by_iid, checked_ids=None):
    fake = SimpleNamespace(
        tree=tree,
        by_iid=by_iid,
        checked_ids=set() if checked_ids is None else set(checked_ids),
        count_var=FakeVar(),
        action_var=FakeVar(),
        bell=lambda: None,
        populate=lambda: (_ for _ in ()).throw(
            AssertionError("selection must not rebuild the table")
        ),
        _is_expired=VoucherApp._is_expired,
    )
    fake._sync_selection_ui = (
        lambda iids=None: VoucherApp._sync_selection_ui(fake, iids)
    )
    return fake


def test_checkbox_click_updates_only_clicked_row_without_populate():
    first = SimpleNamespace(id="voucher-1", status="VALID_MULTI")
    second = SimpleNamespace(id="voucher-2", status="VALID_MULTI")
    tree = FakeTree(
        {
            "row-1": ("☐", "11111-22222", "First"),
            "row-2": ("☐", "33333-44444", "Second"),
        }
    )
    fake = _selection_app(
        tree,
        {
            "row-1": first,
            "row-2": second,
        },
    )

    result = VoucherApp.on_tree_click(
        fake,
        SimpleNamespace(x=4, y=8),
    )

    assert result == "break"
    assert fake.checked_ids == {"voucher-1"}
    assert tree.rows["row-1"][0] == "☑"
    assert tree.rows["row-2"][0] == "☐"
    assert tree.writes == ["row-1"]
    assert fake.count_var.value == "2 visualizzati  •  1 selezionati"
    assert fake.action_var.value == "Stampa selezionati (1)"


def test_toggle_all_visible_updates_marks_in_place_and_skips_expired():
    active = SimpleNamespace(id="active", status="VALID_MULTI")
    expired = SimpleNamespace(id="expired", status="EXPIRED")
    tree = FakeTree(
        {
            "row-1": ("☐", "11111-22222", "Active"),
            "row-2": ("—", "33333-44444", "Expired"),
        }
    )
    fake = _selection_app(
        tree,
        {
            "row-1": active,
            "row-2": expired,
        },
    )

    VoucherApp.toggle_all_visible(fake)

    assert fake.checked_ids == {"active"}
    assert tree.rows["row-1"][0] == "☑"
    assert tree.rows["row-2"][0] == "—"
    assert fake.count_var.value == "2 visualizzati  •  1 selezionati"
    assert fake.action_var.value == "Stampa selezionati (1)"

    VoucherApp.toggle_all_visible(fake)

    assert fake.checked_ids == set()
    assert tree.rows["row-1"][0] == "☐"
    assert tree.rows["row-2"][0] == "—"
    assert fake.count_var.value == "2 visualizzati  •  0 selezionati"
    assert fake.action_var.value == "Stampa selezionati"


class HomeTree:
    def __init__(self):
        self.highlighted = []
        self.selection_set_calls = 0
        self.focused = "home-1"

    def focus_set(self):
        pass

    def focus(self, iid=None):
        if iid is not None:
            self.focused = iid
        return self.focused

    def identify_region(self, _x, _y):
        return "cell"

    def identify_row(self, _y):
        return "home-1"

    def selection_set(self, iids):
        self.selection_set_calls += 1
        self.highlighted = list(iids)


def test_home_click_toggles_one_voucher_without_dropping_hidden_selection():
    visible = SimpleNamespace(id="visible", status="VALID_MULTI")
    hidden = SimpleNamespace(id="hidden", status="VALID_MULTI")
    tree = HomeTree()
    fake = SimpleNamespace(
        home_recent_tree=tree,
        _home_voucher_by_iid={"home-1": visible},
        checked_ids={"hidden"},
        home_print_action_var=FakeVar(),
        bell=lambda: None,
        _is_expired=VoucherApp._is_expired,
        _voucher_alignment_ready=lambda voucher: True,
    )
    fake._sync_selection_ui = (
        lambda iids=None: ModernVoucherApp._sync_home_selection_ui(fake)
    )

    result = ModernVoucherApp._on_home_recent_click(
        fake,
        SimpleNamespace(x=4, y=8),
    )

    assert result == "break"
    assert fake.checked_ids == {"hidden", "visible"}
    assert tree.highlighted == ["home-1"]
    assert tree.selection_set_calls == 1
    assert fake.home_print_action_var.value == "Stampa 2 voucher"


def test_programmatic_home_highlight_is_one_way_and_does_not_call_click_handler():
    visible = SimpleNamespace(id="visible", status="VALID_MULTI")
    tree = HomeTree()
    fake = SimpleNamespace(
        home_recent_tree=tree,
        _home_voucher_by_iid={"home-1": visible},
        checked_ids={"visible"},
        home_print_action_var=FakeVar(),
        _is_expired=VoucherApp._is_expired,
    )

    ModernVoucherApp._sync_home_selection_ui(fake)

    assert tree.highlighted == ["home-1"]
    assert tree.selection_set_calls == 1
    assert fake.checked_ids == {"visible"}


@pytest.mark.parametrize("workspace", ["home", "voucher"])
@pytest.mark.parametrize("status", ["VALID_MULTI", "EXPIRED"])
def test_space_uses_print_selection_and_preserves_hidden_vouchers(workspace, status):
    tree = HomeTree()
    visible = SimpleNamespace(id="visible", status=status)
    calls = []
    fake = SimpleNamespace(
        home_recent_tree=tree if workspace == "home" else HomeTree(),
        _home_voucher_by_iid={"home-1": visible},
        by_iid={"home-1": visible},
        checked_ids={"hidden"},
        _is_expired=VoucherApp._is_expired,
        _voucher_alignment_ready=lambda voucher: True,
        bell=lambda: calls.append("bell"),
        _sync_selection_ui=lambda: calls.append("sync"),
    )
    event = SimpleNamespace(widget=tree)
    assert ModernVoucherApp._on_voucher_selection_key(fake, event) == "break"
    if status == "EXPIRED":
        assert fake.checked_ids == {"hidden"}
        assert calls == ["bell"]
    else:
        assert fake.checked_ids == {"hidden", "visible"}
        assert calls == ["sync"]
        ModernVoucherApp._on_voucher_selection_key(fake, event)
        assert fake.checked_ids == {"hidden"}


def test_native_keyboard_navigation_cannot_change_print_highlighting():
    """Exercise Tk class bindings as well as the application Space binding."""
    import tkinter as tk
    import sys
    from tkinter import ttk

    try:
        root = tk.Tk()
    except tk.TclError:
        if sys.platform == "win32":
            raise
        pytest.skip("Tk display unavailable")
    try:
        tree = ttk.Treeview(root, selectmode="none")
        tree.pack()
        tree.insert("", "end", iid="first", text="First")
        tree.insert("", "end", iid="second", text="Second")
        fake = SimpleNamespace(
            home_recent_tree=tree,
            _home_voucher_by_iid={
                iid: SimpleNamespace(id=iid, status="VALID_MULTI")
                for iid in ("first", "second")
            },
            checked_ids={"first", "hidden"},
            _is_expired=VoucherApp._is_expired,
            _voucher_alignment_ready=lambda voucher: True,
            bell=lambda: None,
        )
        fake._sync_selection_ui = lambda: tree.selection_set(
            [iid for iid in tree.get_children() if iid in fake.checked_ids]
        )
        tree.bind("<space>", lambda event: ModernVoucherApp._on_voucher_selection_key(fake, event))
        root.update()
        tree.focus_force()
        tree.focus("first")
        fake._sync_selection_ui()
        root.update()
        tree.event_generate("<Down>")
        root.update()
        assert tree.focus() == "second"
        assert tree.selection() == ("first",)
        assert fake.checked_ids == {"first", "hidden"}
        tree.event_generate("<space>")
        root.update()
        assert set(tree.selection()) == {"first", "second"}
        assert fake.checked_ids == {"first", "second", "hidden"}
    finally:
        root.destroy()



def test_workspace_print_state_keeps_unknown_out_of_to_print():
    voucher = SimpleNamespace(id="unknown")
    fake = SimpleNamespace(
        _workspace_print_state_by_unifi_id={
            "unknown": (True, "UNKNOWN"),
            "printable": (True, "NOT_PRINTED"),
        }
    )

    assert ModernVoucherApp._workspace_print_state(
        fake,
        voucher,
        None,
    ) == "NON DETERMINABILE"
    assert ModernVoucherApp._workspace_print_state(
        fake,
        SimpleNamespace(id="printable"),
        None,
    ) == "DA STAMPARE"


def test_home_metrics_exclude_unknown_print_and_count_first_multiuse_use():
    fake = SimpleNamespace(
        controller_snapshot_live=True,
        vouchers=[
            SimpleNamespace(
                id="unknown",
                status="VALID_MULTI",
                used=0,
                code_formatted="11111-11111",
                recipient="Unknown print",
                create_time=40,
                end_time=0,
            ),
            SimpleNamespace(
                id="printable",
                status="VALID_MULTI",
                used=0,
                code_formatted="22222-22222",
                recipient="Printable",
                create_time=30,
                end_time=0,
            ),
            SimpleNamespace(
                id="multi-used",
                status="USED_MULTIPLE",
                used=1,
                code_formatted="33333-33333",
                recipient="Multiuse",
                create_time=20,
                end_time=0,
            ),
            SimpleNamespace(
                id="expired",
                status="EXPIRED",
                used=0,
                code_formatted="44444-44444",
                recipient="Expired",
                create_time=10,
                end_time=0,
            ),
        ],
        home_to_print_var=FakeVar(),
        home_active_var=FakeVar(),
        home_used_var=FakeVar(),
        home_expired_var=FakeVar(),
        home_unprinted_alert_var=FakeVar(),
        home_security_alert_var=FakeVar(),
        _is_expired=lambda voucher: voucher.status == "EXPIRED",
        _workspace_print_state=lambda voucher, _stat: {
            "unknown": "NON DETERMINABILE",
            "printable": "DA STAMPARE",
            "multi-used": "STAMPATO",
            "expired": "SCADUTO",
        }[voucher.id],
        _refresh_home_activity=lambda: None,
        _refresh_home_threshold_alerts=lambda: None,
        _refresh_controller_workspace_status=lambda: None,
    )

    ModernVoucherApp._update_operator_summary(fake, {})

    assert fake.home_to_print_var.value == "1"
    assert fake.home_active_var.value == "3"
    assert fake.home_used_var.value == "1"
    assert fake.home_expired_var.value == "1"


def test_unaligned_voucher_cannot_enter_print_selection_from_home():
    visible = SimpleNamespace(id="visible", status="VALID_MULTI")
    tree = HomeTree()
    calls = []
    fake = SimpleNamespace(
        home_recent_tree=tree,
        _home_voucher_by_iid={"home-1": visible},
        checked_ids={"hidden"},
        home_print_action_var=FakeVar(),
        bell=lambda: calls.append("bell"),
        _is_expired=VoucherApp._is_expired,
        _voucher_alignment_ready=lambda voucher: False,
    )
    fake._sync_selection_ui = lambda iids=None: calls.append("sync")

    result = ModernVoucherApp._on_home_recent_click(
        fake,
        SimpleNamespace(x=4, y=8),
    )

    assert result == "break"
    assert fake.checked_ids == {"hidden"}
    assert calls == ["bell"]
