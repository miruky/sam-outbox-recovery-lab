import pytest
from domain import event_id, matches, validate_order


def test_same_count_and_sum_do_not_hide_replaced_event():
    # 件数と合計が同じでも、業務イベントの欠落と置換を拒否する。
    assert not matches({"a": 100, "b": 200}, {"a": 100, "c": 200}, {"order_count": 2, "total_amount": 300})


def test_amounts_must_match_per_event():
    assert not matches({"a": 100, "b": 200}, {"a": 200, "b": 100}, {"order_count": 2, "total_amount": 300})


def test_all_three_conditions_must_match():
    expected = {"a": 100, "b": 200}
    assert matches(expected, dict(expected), {"order_count": 2, "total_amount": 300})
    assert not matches(expected, dict(expected), {"order_count": 3, "total_amount": 300})
    assert not matches(expected, dict(expected), {"order_count": 2, "total_amount": 301})


@pytest.mark.parametrize("amount", [True, 1.1, 0, -1, 1000001])
def test_invalid_amounts_are_rejected(amount):
    with pytest.raises(ValueError):
        validate_order({"order_id": "order-001", "amount": amount})


def test_event_identity_does_not_depend_on_delivery():
    assert event_id("order-001") == event_id("order-001")
    assert event_id("order-001") != event_id("order-002")
