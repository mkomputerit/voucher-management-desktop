from types import SimpleNamespace

from voucher_management.app import VoucherApp


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
    assert fake.action_var.value == "PREPARA STAMPA  (1)"


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
    assert fake.action_var.value == "PREPARA STAMPA  (1)"

    VoucherApp.toggle_all_visible(fake)

    assert fake.checked_ids == set()
    assert tree.rows["row-1"][0] == "☐"
    assert tree.rows["row-2"][0] == "—"
    assert fake.count_var.value == "2 visualizzati  •  0 selezionati"
    assert fake.action_var.value == "PREPARA STAMPA"
