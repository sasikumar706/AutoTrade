"""Regression tests for order-placement / exit fixes (Nov 2026 review)."""
from datetime import datetime, timedelta

from app.positions import ManagedPosition
from app.risk import RiskConfig
from tests.test_options import RISK, FakeKite, TimeoutError_, make_auto, spec


def _filled(k, risk=RISK, lots=1):
    p = ManagedPosition(spec(lots=lots), risk, "live", lambda: k)
    p.open(100.0)
    p.on_order_update({"order_id": p.entry_order_id, "status": "COMPLETE", "average_price": 100.0, "filled_quantity": 65 * lots})
    return p


def test_cutoff_closes_every_open_position_not_just_the_newest():
    a, reg, _ = make_auto(FakeKite(), max_open_positions=2, max_trades_per_day=5, max_daily_loss=100000, max_capital=200000)
    a.start_day(datetime(2026, 10, 28, 9, 0))
    a.enabled, a.last_spot = True, 24010.0
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 35)}, datetime(2026, 10, 28, 9, 40, 1))
    a.execute_signal(1, {"date": datetime(2026, 10, 28, 9, 50)}, datetime(2026, 10, 28, 9, 55, 1))
    assert len(reg) == 2 and a.others == [reg[0]]
    a.on_clock(datetime(2026, 10, 28, 10, 30, 0))
    assert all(p.state == "POSITION_CLOSED" and p.exit_reason == "time_cutoff" for p in reg)


def test_trailing_stop_moves_app_side_when_broker_sl_missing():
    k = FakeKite()
    p = _filled(k, RiskConfig(sl_value=20, target_mode="none", trail_mode="pct", trail_value=10, trail_activate_rr=0.5))
    p.on_order_update({"order_id": p.sl_order_id, "status": "REJECTED", "status_message": "x"})
    assert p.sl_order_id is None
    p.on_tick(130.0)
    assert p.stop == 117.0 and not k.modified  # before: modify_order(order_id=None) was sent (Kite rejects it) and the stop stayed


def test_stop_breach_without_broker_sl_exits_at_once():
    k = FakeKite()
    p = _filled(k)
    p.on_order_update({"order_id": p.sl_order_id, "status": "REJECTED", "status_message": "x"})
    p.on_tick(75.0, now=1000.0)
    assert p.state == "EXIT_PENDING" and k.orders_placed[-1]["order_type"] == "MARKET"


def test_exit_never_sells_the_part_a_pending_partial_order_sells():
    k = FakeKite()
    p = _filled(k, lots=2)
    p.ltp = 120.0
    assert p.book_partial(65, "scale_out")
    p.request_exit("manual")  # partial still OPEN in the book
    assert k.orders_placed[-1]["quantity"] == 65  # not 130


def test_partial_that_already_filled_is_booked_before_exit():
    k = FakeKite()
    p = _filled(k, lots=2)
    p.ltp = 120.0
    p.book_partial(65, "scale_out")
    k.book[p.partial_order_id].update(status="COMPLETE", average_price=120.0, filled_quantity=65, tag=p.spec.tag)
    p.request_exit("manual")
    assert p.booked and p.filled_qty == 65 and k.orders_placed[-1]["quantity"] == 65


def test_uncertain_exit_adopts_order_found_in_kite_instead_of_selling_twice():
    k = FakeKite()
    p = _filled(k)
    for o in k.book.values():
        o["tag"] = p.spec.tag
    k.fail_next = TimeoutError_("Gateway timed out")
    p.request_exit("manual")
    sells = [o for o in k.orders_placed if o["transaction_type"] == "SELL" and o["order_type"] == "MARKET"]
    assert len(sells) == 1 and p.exit_order_id is not None and p.state == "EXIT_PENDING"
    k.book[p.exit_order_id].update(status="COMPLETE", average_price=110.0)
    p.reconcile()
    assert p.state == "POSITION_CLOSED" and p.exit_price == 110.0


def test_uncertain_entry_ignores_older_orders_in_same_contract():
    k = FakeKite()
    k.book["OLD"] = {"order_id": "OLD", "status": "COMPLETE", "transaction_type": "BUY", "order_type": "MARKET",
                     "tradingsymbol": spec().tradingsymbol, "tag": "kitealgo", "average_price": 90.0, "filled_quantity": 65,
                     "order_timestamp": datetime.now() - timedelta(hours=1)}
    k.fail_next = RuntimeError("Gateway timed out")
    p = ManagedPosition(spec(), RISK, "live", lambda: k)
    p.open(100.0)
    new = [o for o in k.book.values() if o["order_id"] != "OLD"][0]
    new.update(tag=p.spec.tag, order_timestamp=datetime.now())
    p.entry_order_id = None
    p.set_state("RECONCILIATION_REQUIRED", "test")
    p.reconcile()
    assert p.entry_order_id == new["order_id"]


def test_unfilled_limit_entry_is_repriced_once_then_cancelled():
    k = FakeKite(premium=100.0, spread=0.5)
    a, _, _ = make_auto(k, mode="paper")
    a.start_day(datetime(2026, 10, 28, 9, 0))
    p = ManagedPosition(spec(), RISK, "live", lambda: k)
    p.entry_band_pts = 0.5
    p.open(100.25)  # limit 100.75
    p.created = datetime(2026, 10, 28, 9, 40).isoformat()
    a.position, a.enabled = p, True
    k.premium = 101.0  # ask 101.25 -> re-price to 101.75 (within 3 bands of the quote)
    a.on_clock(datetime(2026, 10, 28, 9, 40, 40))
    assert k.modified and k.modified[-1]["price"] == 101.75 and p.entry_limit == 101.75
    k.premium = 102.0
    a.on_clock(datetime(2026, 10, 28, 9, 41, 0))
    assert len(k.modified) == 1  # only once
    a.on_clock(datetime(2026, 10, 28, 9, 42))
    assert getattr(p, "cancel_sent", False)


def test_runaway_limit_entry_is_not_chased():
    k = FakeKite(premium=100.0, spread=0.5)
    a, _, _ = make_auto(k, mode="paper")
    a.start_day(datetime(2026, 10, 28, 9, 0))
    p = ManagedPosition(spec(), RISK, "live", lambda: k)
    p.entry_band_pts = 0.5
    p.open(100.25)
    p.created = datetime(2026, 10, 28, 9, 40).isoformat()
    a.position, a.enabled = p, True
    k.premium = 110.0
    a.on_clock(datetime(2026, 10, 28, 9, 40, 40))
    assert not k.modified


def test_entry_band_scales_with_premium():
    a, _, _ = make_auto(FakeKite(), entry_tolerance_pts=1.0)
    assert a._entry_band(100.0) == 1.0
    assert a._entry_band(600.0) == 3.0
    b, _, _ = make_auto(FakeKite(), entry_tolerance_pts=0.0)
    assert b._entry_band(600.0) == 0.0


def test_swing_roll_counts_the_old_contracts_pnl_when_its_sell_fills_later():
    from app.swing import SwingConfig, SwingTrader
    k = FakeKite()
    w = SwingTrader(SwingConfig(), None, lambda p: None)
    old = _filled(k)
    w.position = old
    old.request_exit("roll")  # live: sell in flight
    w.closing = [old]
    w.position = _filled(k)  # the new monthly is bought
    old.on_order_update({"order_id": old.exit_order_id, "status": "COMPLETE", "average_price": 120.0})
    w.settle()
    assert w.realized == round(old.net, 2) and old.net > 0 and w.closing == []
