"""ManagedPosition: one option (or stock) position with a persistent state machine.

States: ENTRY_PENDING -> POSITION_OPEN (or PARTIALLY_FILLED) -> EXIT_PENDING -> POSITION_CLOSED
        plus ORDER_REJECTED, EXIT_FAILED, RECONCILIATION_REQUIRED.

LIVE rules (Kite):
* An order is NEVER assumed filled because it was submitted; fills come from order updates
  (WebSocket / postback) or from reconciliation against Kite's order book.
* After the entry fills, a broker-side protective SL order is placed at the stop, so the
  position stays protected even if this server or the phone dies.
* Target / time-cutoff / manual exits cancel the SL order first, then send a MARKET exit
  with market_protection (mandatory for API market orders since 1 Apr 2026).
* If an exit fails, the position goes to EXIT_FAILED, the user is alerted, and the exit is
  retried with back-off; it is never silently marked closed.
* If an order call fails in an uncertain way (timeout), we reconcile with the order book
  instead of blindly re-sending (prevents duplicate orders).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Callable

from .charges import round_trip_charges, segment_for
from .risk import RiskConfig, RiskError, compute_levels, locked_rs, round_tick, trail_stop, validate_position

OPEN_STATES = ("POSITION_OPEN", "PARTIALLY_FILLED")
ACTIVE_STATES = ("ENTRY_PENDING", "POSITION_OPEN", "PARTIALLY_FILLED", "EXIT_PENDING", "EXIT_FAILED", "RECONCILIATION_REQUIRED")
SL_LIMIT_BUFFER = 0.05  # SL (stop-limit) order limit price is 5% beyond the trigger
SL_FILL_GRACE_S = 8  # if price is beyond the stop this long without a fill -> recovery exit
MAX_SL_MODIFICATIONS = 20  # Kite allows 25 modifications per order
MAX_EXIT_RETRIES = 3


@dataclass
class PositionSpec:
    exchange: str
    tradingsymbol: str
    instrument_token: int
    lots: int
    lot_size: int
    side: int  # +1 buy to open (long), -1 sell to open (short)
    product: str = "MIS"
    tick_size: float = 0.05
    instrument_type: str = "CE"
    strike: float | None = None
    expiry: str | None = None
    source: str = "manual"  # manual | auto:<id>
    tag: str = "kitealgo"

    @property
    def qty(self) -> int:
        return self.lots * self.lot_size


class ManagedPosition:
    def __init__(self, spec: PositionSpec, risk: RiskConfig, mode: str, kite_getter: Callable[[], Any] | None = None,
                 on_change: Callable[["ManagedPosition"], None] | None = None, slippage_bps: float = 30.0):
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        self.id = uuid.uuid4().hex[:10]
        self.spec, self.risk, self.mode = spec, risk, mode
        self.kite_getter = kite_getter
        self.on_change = on_change
        self.slip = slippage_bps / 10_000
        self.lock = threading.RLock()
        self.state = "NEW"
        self.message = ""
        self.ref_price: float | None = None
        self.entry_band_pts: float | None = None  # set -> LIMIT entry at ref ± this many ₹ instead of MARKET
        self.entry_limit: float | None = None
        self.entry_price: float | None = None
        self.exit_price: float | None = None
        self.filled_qty = 0
        self.stop: float | None = None
        self.stop0: float | None = None
        self.target: float | None = None
        self.risk_unit = 0.0
        self.best: float | None = None
        self.ltp: float | None = None
        self.entry_order_id: str | None = None
        self.sl_order_id: str | None = None
        self.exit_order_id: str | None = None
        self.exit_reason: str | None = None
        self.exit_retries = 0
        self.sl_mods = 0
        self.beyond_stop_since: float | None = None
        self.next_retry_at = 0.0
        self.gross = self.charges = self.net = 0.0
        self.created = datetime.now().isoformat()
        self.entry_time: str | None = None
        self.exit_time: str | None = None
        self.events: list[dict] = []
        self.path: list[list] = []  # [["HH:MM:SS", premium], ...] sampled every ~20 s, for the position chart
        self.booked: list[dict] = []  # partial exits (scale-out): [{qty, price, gross, charges, time, reason}]
        self.partial_order_id: str | None = None
        self.partial_pending: dict | None = None
        self.initial_qty = 0
        self._path_at = 0.0
        self.stats = {"orders_sent": 0, "orders_rejected": 0, "orders_filled": 0, "sl_order_fills": 0,
                      "stop_recoveries": 0, "target_exits": 0, "entry_slippage_pct": None, "exit_slippage_pct": None}

    # ------------------------------------------------------------ helpers
    def event(self, kind: str, msg: str, **data):
        self.events.append({"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, "msg": msg, **data})
        self.events = self.events[-200:]
        if self.on_change:
            try:
                self.on_change(self)
            except Exception:
                pass

    def set_state(self, st: str, msg: str = ""):
        self.state = st
        if msg:
            self.message = msg
        self.event("state", f"{st}{': ' + msg if msg else ''}")

    @property
    def kite(self):
        return self.kite_getter() if self.kite_getter else None

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    def _order(self, **kw) -> str:
        k = self.kite
        self.stats["orders_sent"] += 1
        return str(k.place_order(variety="regular", exchange=self.spec.exchange, tradingsymbol=self.spec.tradingsymbol,
                                 product=self.spec.product, validity="DAY", tag=self.spec.tag[:20], **kw))

    def _txn(self, side: int) -> str:
        return "BUY" if side > 0 else "SELL"

    # ------------------------------------------------------------ entry
    def preview(self, ref_price: float) -> dict:
        """Levels/qty/risk for a proposed entry without placing anything (raises RiskError)."""
        lv = compute_levels(self.risk, ref_price, self.spec.side, self.spec.tick_size)
        problems = validate_position(self.risk, lv, self.spec.qty)
        if problems:
            raise RiskError("; ".join(problems))
        cap = self.stats.get("target_cap_pts")
        if cap and lv.target is not None and (lv.target - ref_price) * self.spec.side > cap:
            t = round_tick(ref_price + self.spec.side * cap, self.spec.tick_size)
            lv = replace(lv, target=t, reward_per_unit=round(cap, 2))
        seg = segment_for(self.spec.product, self.spec.instrument_type)
        est = round_trip_charges(seg, ref_price * self.spec.qty, (lv.target or ref_price) * self.spec.qty,
                                 "BSE" if self.spec.exchange in ("BSE", "BFO") else "NSE")["total"]
        return {"entry": round(ref_price, 2), "stop_loss": lv.stop, "target": lv.target, "qty": self.spec.qty,
                "lots": self.spec.lots, "risk_reward": lv.rr, "max_loss": round(lv.risk_per_unit * self.spec.qty, 2),
                "target_profit": round((lv.reward_per_unit or 0) * self.spec.qty, 2),
                "order_value": round(ref_price * self.spec.qty, 2), "est_charges": round(est, 2)}

    def open(self, ref_price: float, ts: datetime | None = None):
        with self.lock:
            if self.state != "NEW":
                raise RuntimeError("Position already submitted")  # duplicate-entry guard
            self.preview(ref_price)  # validates mandatory SL / risk budget before anything is sent
            self.ref_price = ref_price
            side = self.spec.side
            if self.entry_band_pts is not None:
                # worst price we accept: up to band ₹ above the quote when buying, below when selling
                self.entry_limit = max(round_tick(ref_price + side * self.entry_band_pts, self.spec.tick_size),
                                       self.spec.tick_size)
            if self.mode == "paper":
                self.state = "ENTRY_PENDING"
                px = ref_price * (1 + self.slip * side)
                if self.entry_limit is not None:
                    px = min(px, self.entry_limit) if side > 0 else max(px, self.entry_limit)
                self._entry_filled(px, self.spec.qty, ts)
                return
            self.set_state("ENTRY_PENDING", "Entry order sent")
            try:
                if self.entry_limit is not None:
                    self.entry_order_id = self._order(transaction_type=self._txn(side), quantity=self.spec.qty,
                                                      order_type="LIMIT", price=self.entry_limit)
                    self.event("order", f"Entry LIMIT order {self.entry_order_id} at {self.entry_limit} "
                                        f"(quote {ref_price:.2f} {'+' if side > 0 else '−'} ₹{self.entry_band_pts:g})", order_id=self.entry_order_id)
                else:
                    self.entry_order_id = self._order(transaction_type=self._txn(side), quantity=self.spec.qty,
                                                      order_type="MARKET", market_protection=-1)
                    self.event("order", f"Entry order {self.entry_order_id} sent", order_id=self.entry_order_id)
            except Exception as e:
                if _is_uncertain(e):
                    self.set_state("RECONCILIATION_REQUIRED", f"Entry submission uncertain ({e}); checking order book")
                    self.reconcile()
                else:
                    self.stats["orders_rejected"] += 1
                    self.set_state("ORDER_REJECTED", f"Entry refused: {e}")

    def _entry_filled(self, price: float, qty: int, ts: datetime | None = None, partial: bool = False):
        self.entry_price = price
        self.filled_qty = qty
        self.entry_time = (ts or datetime.now()).isoformat()
        self.stats["orders_filled"] += 1
        if self.ref_price:
            self.stats["entry_slippage_pct"] = round((price - self.ref_price) / self.ref_price * 100 * self.spec.side, 3)
        try:
            lv = compute_levels(self.risk, price, self.spec.side, self.spec.tick_size)
            stop, target, dist = lv.stop, lv.target, lv.risk_per_unit
        except RiskError:
            # fill moved far from the reference: keep the planned stop distance, measured from the fill
            ref = compute_levels(self.risk, self.ref_price, self.spec.side, self.spec.tick_size)
            dist = ref.risk_per_unit
            stop = round_tick(price - self.spec.side * dist, self.spec.tick_size)
            target = round_tick(price + self.spec.side * ref.reward_per_unit, self.spec.tick_size) if ref.reward_per_unit else None
        cap = self.stats.get("target_cap_pts")
        if cap and target is not None and (target - price) * self.spec.side > cap:
            target = round_tick(price + self.spec.side * cap, self.spec.tick_size)  # e.g. just before a big OI wall
        self.stop = self.stop0 = stop
        self.target = target
        self.risk_unit = dist
        self.initial_qty = qty
        self.stats["risk_pts"] = round(float(dist), 2)
        self.stats["target_pts"] = round(abs(target - price), 2) if target is not None else None
        self.stats["mfe_pts"] = self.stats["mae_pts"] = 0.0
        self.best = price
        self.set_state("PARTIALLY_FILLED" if partial else "POSITION_OPEN",
                       f"{'Partially filled' if partial else 'Filled'} {qty} @ {price:.2f}, stop {self.stop}, target {self.target}")
        if self.mode == "live":
            self._place_protective_sl()

    def _place_protective_sl(self):
        side = -self.spec.side
        trig = self.stop
        price = round_tick(trig * (1 - SL_LIMIT_BUFFER) if side < 0 else trig * (1 + SL_LIMIT_BUFFER), self.spec.tick_size)
        try:
            self.sl_order_id = self._order(transaction_type=self._txn(side), quantity=self.filled_qty, order_type="SL",
                                           trigger_price=trig, price=max(price, self.spec.tick_size))
            self.event("order", f"Protective SL order {self.sl_order_id} at trigger {trig}", order_id=self.sl_order_id)
        except Exception as e:
            self.stats["orders_rejected"] += 1
            self.event("alert", f"PROTECTIVE STOP ORDER FAILED ({e}). App is watching the stop; check Kite.")
            self.message = "Protective stop order failed — app-side stop monitoring only"

    # ------------------------------------------------------------ live data
    def on_tick(self, ltp: float, now: float | None = None):
        now = now or time.time()
        with self.lock:
            self.ltp = ltp
            if self.state in ("POSITION_OPEN", "PARTIALLY_FILLED") and now - self._path_at >= 20:
                self._path_at = now
                self.path.append([datetime.now().strftime("%H:%M:%S"), round(float(ltp), 2)])
                self.path = self.path[-600:]
            if self.state == "EXIT_FAILED" and now >= self.next_retry_at:
                self._send_exit(self.exit_reason or "retry")
                return
            if not self.is_open:
                return
            s = self.spec.side
            if self.entry_price:
                move = (ltp - self.entry_price) * s  # how far it went for (+) / against (-) us while open
                if move > (self.stats.get("mfe_pts") or 0):
                    self.stats["mfe_pts"] = round(move, 2)
                if move < (self.stats.get("mae_pts") or 0):
                    self.stats["mae_pts"] = round(move, 2)
            hit_stop = (ltp <= self.stop) if s > 0 else (ltp >= self.stop)
            hit_target = self.target is not None and ((ltp >= self.target) if s > 0 else (ltp <= self.target))
            if hit_stop:
                reason = "stop_loss" if self.stop == self.stop0 else ("profit_lock" if self.stats.get("profit_locked")
                                                                     and abs(self.stop - self._lock_price()) < 1e-6 else "trailing_stop")
                if self.mode == "paper":
                    self._close(min(ltp, self.stop) if s > 0 else max(ltp, self.stop), reason)
                    return
                # live: broker SL order should fill; recover if it doesn't
                if self.beyond_stop_since is None and self.sl_order_id:
                    self.beyond_stop_since = now  # give the exchange SL order a few seconds to fill
                elif not self.sl_order_id or now - self.beyond_stop_since >= SL_FILL_GRACE_S:
                    self.stats["stop_recoveries"] += 1
                    self.event("alert", "Stop breached but SL order not filled — sending recovery market exit")
                    self.request_exit(reason)
                return
            self.beyond_stop_since = None
            if hit_target:
                self.stats["target_exits"] += 1
                self.request_exit("quick_target" if self.stats.get("quick_target") else "target")
                return
            # trailing stop (only ever tightens)
            self.best = max(self.best, ltp) if s > 0 else min(self.best, ltp)
            new = trail_stop(self.risk, s, self.entry_price, self.stop, self.risk_unit, self.best, self.spec.tick_size,
                             self.spec.lot_size)
            keep = locked_rs(self.risk, (self.best - self.entry_price) * s * self.spec.lot_size)
            if keep > (self.stats.get("locked_rs") or 0):
                self.stats["profit_locked"], self.stats["locked_rs"] = True, keep
                self.event("info", f"Profit lock: 1 lot was up ₹{(self.best - self.entry_price) * s * self.spec.lot_size:,.0f} — "
                                   f"stop moved to keep ₹{keep:,.0f} per lot (sells if profit falls back to that)")
            if new != self.stop:
                improve = abs(new - self.stop)
                if self.mode == "paper":
                    self.stop = new
                elif improve >= max(2 * self.spec.tick_size, self.entry_price * 0.005) and self.sl_mods < MAX_SL_MODIFICATIONS:
                    self._modify_sl(new)

    def _lock_price(self) -> float:
        lot = self.spec.lot_size or 1
        return round_tick(self.entry_price + self.spec.side * (self.stats.get("locked_rs") or 0) / lot, self.spec.tick_size)

    def _modify_sl(self, new_stop: float):
        if not self.sl_order_id:  # no exchange SL order (rejected / not placed): the app-side stop still trails
            self.stop = new_stop
            self.event("trail", f"Trailing stop moved to {new_stop} (app-side)")
            return
        side = -self.spec.side
        price = round_tick(new_stop * (1 - SL_LIMIT_BUFFER) if side < 0 else new_stop * (1 + SL_LIMIT_BUFFER), self.spec.tick_size)
        try:
            self.kite.modify_order(variety="regular", order_id=self.sl_order_id, trigger_price=new_stop, price=price, order_type="SL")
            self.sl_mods += 1
            self.stop = new_stop
            self.event("trail", f"Trailing stop moved to {new_stop}")
        except Exception as e:
            self.event("alert", f"Could not move trailing stop ({e}); previous stop still active")

    def tighten_stop(self, new_stop: float, why: str) -> bool:
        """Move the stop closer to the price (never loosens). Live: modifies the exchange SL order."""
        with self.lock:
            if not self.is_open or self.stop is None:
                return False
            new_stop = round_tick(new_stop, self.spec.tick_size)
            better = new_stop > self.stop if self.spec.side > 0 else new_stop < self.stop
            if not better:
                return False
            if self.mode == "live" and self.sl_order_id:
                self._modify_sl(new_stop)
            else:
                self.stop = new_stop
            self.event("trail", f"Stop moved to {new_stop} — {why}")
            return True

    def lower_target(self, new_target: float, why: str) -> bool:
        """Bring the target closer to the entry (a smaller, quicker profit). Never moves it further away."""
        with self.lock:
            if not self.is_open or self.target is None:
                return False
            new_target = round_tick(new_target, self.spec.tick_size)
            closer = new_target < self.target if self.spec.side > 0 else new_target > self.target
            if not closer:
                return False
            self.target = new_target
            self.event("risk", f"Target moved to {new_target} — {why}")
            return True

    def lift_target(self, why: str) -> bool:
        """Remove the fixed target so a strong move is ridden with the (trailing) stop instead."""
        with self.lock:
            if not self.is_open or self.target is None:
                return False
            self.target = None
            self.event("risk", f"Target removed — {why}")
            return True

    # ------------------------------------------------------------ exit
    def book_partial(self, qty: int, reason: str) -> bool:
        """Scale out: sell part of the position now (e.g. half the lots at +1R) and keep the rest running.
        Live: the protective SL order is first cut to the remaining quantity, then a market order sells `qty`."""
        with self.lock:
            if not self.is_open or qty <= 0 or qty >= self.filled_qty or self.partial_order_id or self.ltp is None:
                return False
            if self.mode == "paper":
                px = self.ltp * (1 - self.slip * self.spec.side)
                self._booked(qty, px, reason)
                return True
            keep = self.filled_qty - qty
            try:
                if self.sl_order_id:
                    self.kite.modify_order(variety="regular", order_id=self.sl_order_id, quantity=keep)
                self.partial_order_id = self._order(transaction_type=self._txn(-self.spec.side), quantity=qty,
                                                    order_type="MARKET", market_protection=-1)
                self.partial_pending = {"qty": qty, "reason": reason}
                self.stats["partial_ids"] = (self.stats.get("partial_ids") or []) + [self.partial_order_id]
                self.event("order", f"Partial exit order {self.partial_order_id}: selling {qty} ({reason})", order_id=self.partial_order_id)
                return True
            except Exception as e:
                self.event("alert", f"Partial exit not sent ({e}); full position still protected")
                if self.sl_order_id:
                    try:
                        self.kite.modify_order(variety="regular", order_id=self.sl_order_id, quantity=self.filled_qty)
                    except Exception:
                        pass
                return False

    def _booked(self, qty: int, price: float, reason: str):
        g, ch = self._leg(qty, price)
        self.booked.append({"qty": qty, "price": round(price, 2), "gross": round(g, 2), "charges": round(ch, 2),
                            "time": datetime.now().isoformat(timespec="seconds"), "reason": reason})
        self.filled_qty -= qty
        self.event("exit", f"Booked {qty} @ {price:.2f} ({reason}): ₹{g - ch:,.0f} net — {self.filled_qty} still running")
        if self.on_change:
            try:
                self.on_change(self)
            except Exception:
                pass

    def request_exit(self, reason: str):
        with self.lock:
            if self.state not in OPEN_STATES and self.state != "EXIT_FAILED":
                return  # duplicate exit guard
            self.exit_reason = reason
            if self.mode == "paper":
                self._close((self.ltp or self.entry_price) * (1 - self.slip * self.spec.side), reason)
                return
            self.set_state("EXIT_PENDING", f"Exiting ({reason})")
            if self.partial_pending:  # a scale-out order is in flight: settle it first so we never sell more than we hold
                self.reconcile()
                if self.state == "POSITION_CLOSED":
                    return
                self.state = "EXIT_PENDING"
            if self.sl_order_id:
                try:
                    self.kite.cancel_order(variety="regular", order_id=self.sl_order_id)
                    self.event("order", f"Cancelled SL order {self.sl_order_id}")
                except Exception as e:
                    # maybe it just filled -> let reconciliation decide instead of double-selling
                    self.event("alert", f"SL cancel failed ({e}); reconciling before exit")
                    if self.reconcile() == "closed":
                        return
            self._send_exit(reason)

    def _pending_partial_qty(self) -> int:
        return int((self.partial_pending or {}).get("qty") or 0) if self.mode == "live" else 0

    def _send_exit(self, reason: str):
        qty = self.filled_qty - self._pending_partial_qty()  # never sell the part a pending scale-out order is selling
        if qty <= 0:
            self.event("alert", "Exit skipped: the pending partial-exit order covers the whole position")
            return
        try:
            self.exit_order_id = self._order(transaction_type=self._txn(-self.spec.side), quantity=qty,
                                             order_type="MARKET", market_protection=-1)
            self.state = "EXIT_PENDING"
            self.event("order", f"Exit order {self.exit_order_id} sent ({reason})", order_id=self.exit_order_id)
        except Exception as e:
            if _is_uncertain(e):
                self.set_state("RECONCILIATION_REQUIRED", f"Exit submission uncertain ({e})")
                self.reconcile()
                return
            self.stats["orders_rejected"] += 1
            self.exit_retries += 1
            self.next_retry_at = time.time() + 5 * self.exit_retries
            self.set_state("EXIT_FAILED", f"EXIT FAILED ({e}). Retrying; close it in Kite if this persists!")

    def _leg(self, qty: int, price: float) -> tuple[float, float]:
        s = self.spec.side
        gross = (price - self.entry_price) * qty * s
        buy_val = (self.entry_price if s > 0 else price) * qty
        sell_val = (price if s > 0 else self.entry_price) * qty
        seg = segment_for(self.spec.product, self.spec.instrument_type)
        ch = round_trip_charges(seg, buy_val, sell_val, "BSE" if self.spec.exchange in ("BSE", "BFO") else "NSE")["total"]
        return gross, ch

    def _close(self, price: float, reason: str):
        pend = self._pending_partial_qty()
        if pend:
            self.event("alert", f"Partial-exit order for {pend} still unconfirmed at close — check it in Kite")
            self.filled_qty -= pend
        q = self.filled_qty
        self.exit_price = price
        self.exit_reason = reason
        self.exit_time = datetime.now().isoformat()
        g, ch = self._leg(q, price) if q else (0.0, 0.0)
        self.gross = g + sum(b["gross"] for b in self.booked)
        self.charges = ch + sum(b["charges"] for b in self.booked)
        if self.booked:  # whole-trade average exit price across the partial bookings and the final exit
            tot = q + sum(b["qty"] for b in self.booked)
            self.exit_price = round((price * q + sum(b["price"] * b["qty"] for b in self.booked)) / tot, 2) if tot else price
            self.filled_qty = tot
        self.net = self.gross - self.charges
        if reason in ("stop_loss", "trailing_stop") and self.mode == "live":
            self.stats["sl_order_fills"] += 1
        self.set_state("POSITION_CLOSED", f"Closed @ {price:.2f} ({reason}) net ₹{self.net:,.2f}")

    def on_order_update(self, d: dict):
        oid = str(d.get("order_id"))
        st = d.get("status")
        with self.lock:
            if oid == self.entry_order_id and self.state in ("ENTRY_PENDING", "RECONCILIATION_REQUIRED"):
                filled = int(d.get("filled_quantity") or 0)
                if st == "COMPLETE":
                    self._entry_filled(float(d.get("average_price")), filled or self.spec.qty)
                elif st in ("REJECTED", "CANCELLED"):
                    if filled > 0:
                        self._entry_filled(float(d.get("average_price")), filled, partial=True)
                    else:
                        self.stats["orders_rejected"] += 1
                        self.set_state("ORDER_REJECTED", f"Entry {st}: {d.get('status_message') or ''}".strip())
            elif oid == self.sl_order_id and self.state in OPEN_STATES + ("EXIT_PENDING",):
                if st == "COMPLETE":
                    self.sl_order_id = None
                    self._close(float(d.get("average_price")), "stop_loss" if self.stop == self.stop0 else "trailing_stop")
                elif st == "REJECTED":
                    self.sl_order_id = None
                    self.event("alert", f"Protective SL order REJECTED: {d.get('status_message')}. App-side stop active.")
            elif oid == self.partial_order_id and self.partial_pending:
                if st == "COMPLETE":
                    pp, self.partial_pending, self.partial_order_id = self.partial_pending, None, None
                    self._booked(int(d.get("filled_quantity") or pp["qty"]), float(d.get("average_price")), pp["reason"])
                elif st in ("REJECTED", "CANCELLED"):
                    self.partial_pending, self.partial_order_id = None, None
                    self.event("alert", f"Partial exit {st}: {d.get('status_message') or ''} — full position kept, stop restored")
                    if self.sl_order_id:
                        try:
                            self.kite.modify_order(variety="regular", order_id=self.sl_order_id, quantity=self.filled_qty)
                        except Exception as e:
                            self.event("alert", f"Could not restore SL quantity ({e}); app-side stop still watching")
            elif oid == self.exit_order_id and self.state in ("EXIT_PENDING", "EXIT_FAILED", "RECONCILIATION_REQUIRED"):
                if st == "COMPLETE":
                    self._close(float(d.get("average_price")), self.exit_reason or "exit")
                elif st in ("REJECTED", "CANCELLED"):
                    self.stats["orders_rejected"] += 1
                    self.exit_retries += 1
                    if self.exit_retries > MAX_EXIT_RETRIES:
                        self.set_state("EXIT_FAILED", f"Exit {st} {self.exit_retries}x — CLOSE THIS POSITION IN KITE NOW")
                        self.next_retry_at = float("inf")
                    else:
                        self.next_retry_at = time.time() + 5 * self.exit_retries
                        self.set_state("EXIT_FAILED", f"Exit {st}: {d.get('status_message') or ''} — retrying")

    def reconcile(self) -> str:
        """Use Kite's order book as the source of truth. Returns 'closed' | 'open' | 'none' | 'unknown'."""
        if self.mode != "live":
            return "open" if self.is_open else "none"
        try:
            orders = self.kite.orders()
        except Exception as e:
            self.event("alert", f"Reconciliation failed ({e}); will retry")
            return "unknown"
        mine = [o for o in orders if o.get("tradingsymbol") == self.spec.tradingsymbol and (o.get("tag") or "") == self.spec.tag[:20]]
        by_id = {str(o["order_id"]): o for o in mine}
        dead = ("REJECTED", "CANCELLED")
        if self.entry_order_id is None and self.state == "RECONCILIATION_REQUIRED" and self.entry_price is None:
            # only orders placed after this position was created (an earlier trade in the same contract is not ours)
            entries = [o for o in mine if o.get("transaction_type") == self._txn(self.spec.side)
                       and o.get("order_type") in ("MARKET", "LIMIT") and _placed_after(o, self.created)]
            if entries:
                self.entry_order_id = str(entries[-1]["order_id"])
                by_id[self.entry_order_id] = entries[-1]
            else:
                self.set_state("ORDER_REJECTED", "Entry order not found in Kite order book — nothing was placed")
                return "none"
        if self.state == "RECONCILIATION_REQUIRED" and self.entry_price is not None and self.exit_order_id is None:
            # an exit whose submission timed out may still have reached Kite: adopt it instead of selling twice
            known = {self.entry_order_id, self.sl_order_id, self.partial_order_id, *(self.stats.get("partial_ids") or [])}
            exits = [o for o in mine if o.get("transaction_type") == self._txn(-self.spec.side) and o.get("order_type") == "MARKET"
                     and str(o["order_id"]) not in known and o.get("status") not in dead and _placed_after(o, self.entry_time)]
            if exits:
                self.exit_order_id = str(exits[-1]["order_id"])
                self.event("order", f"Reconciled: found exit order {self.exit_order_id} in Kite", order_id=self.exit_order_id)
        for oid in (self.entry_order_id, self.sl_order_id, self.partial_order_id, self.exit_order_id):
            if oid and oid in by_id:
                self.on_order_update(by_id[oid])
        if self.state == "RECONCILIATION_REQUIRED" and self.exit_order_id and self.entry_price is not None:
            self.state = "EXIT_PENDING"  # exit order is live in Kite: wait for its fill
        if self.state == "POSITION_CLOSED":
            return "closed"
        if self.state == "RECONCILIATION_REQUIRED" and self.exit_order_id is None and self.entry_price is not None:
            # exit was uncertain and no exit order exists -> still open
            self.set_state("POSITION_OPEN", "Reconciled: position still open")
        return "open" if self.is_open or self.state == "EXIT_PENDING" else self.state.lower()

    # ------------------------------------------------------------ persistence
    def unrealized(self) -> float:
        if not self.is_open or self.ltp is None or self.entry_price is None:
            return 0.0
        return (self.ltp - self.entry_price) * self.filled_qty * self.spec.side

    def snapshot(self) -> dict:
        return {
            "id": self.id, "mode": self.mode, "state": self.state, "message": self.message,
            "spec": {**asdict(self.spec), "qty": self.spec.qty}, "risk": self.risk.to_dict(),
            "ref_price": self.ref_price, "entry_price": _r(self.entry_price), "exit_price": _r(self.exit_price),
            "filled_qty": self.filled_qty, "stop": self.stop, "stop0": self.stop0, "target": self.target,
            "ltp": self.ltp, "unrealized": round(self.unrealized(), 2),
            "booked_net": round(sum(b["gross"] - b["charges"] for b in self.booked), 2),
            "gross": round(self.gross, 2), "charges": round(self.charges, 2), "net": round(self.net, 2),
            "entry_order_id": self.entry_order_id, "sl_order_id": self.sl_order_id, "exit_order_id": self.exit_order_id,
            "exit_reason": self.exit_reason, "exit_retries": self.exit_retries, "sl_mods": self.sl_mods,
            "created": self.created, "entry_time": self.entry_time, "exit_time": self.exit_time,
            "entry_limit": self.entry_limit,
            "risk_unit": self.risk_unit, "best": self.best,
            "stats": self.stats, "events": self.events[-60:], "path": self.path,
            "booked": self.booked, "partial_order_id": self.partial_order_id, "partial_pending": self.partial_pending,
            "initial_qty": self.initial_qty,
        }

    @classmethod
    def restore(cls, snap: dict, kite_getter=None, on_change=None) -> "ManagedPosition":
        spec_d = {k: v for k, v in snap["spec"].items() if k != "qty"}
        p = cls(PositionSpec(**spec_d), RiskConfig.from_dict(snap["risk"]), snap["mode"], kite_getter, None)
        p.id = snap["id"]
        for k in ("state", "message", "ref_price", "entry_price", "exit_price", "filled_qty", "stop", "stop0", "target",
                  "ltp", "gross", "charges", "net", "entry_order_id", "sl_order_id", "exit_order_id", "exit_reason",
                  "exit_retries", "sl_mods", "created", "entry_time", "exit_time", "risk_unit", "best", "stats", "events", "entry_limit",
                  "path", "booked", "partial_order_id", "partial_pending", "initial_qty"):
            if k in snap:
                setattr(p, k, snap[k])
        p.best = p.best if p.best is not None else p.entry_price
        p.on_change = on_change
        return p


def _r(x):
    return round(x, 2) if isinstance(x, (int, float)) else x


def _placed_after(o: dict, since: str | None, slack_s: float = 120.0) -> bool:
    """True when a Kite order was placed after `since` (iso time), with some clock slack. Unknown times count as True."""
    ts = o.get("order_timestamp") or o.get("exchange_timestamp")
    if not ts or not since:
        return True
    try:
        t = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts).replace(" ", "T"))
        s0 = datetime.fromisoformat(since)
        return (t.replace(tzinfo=None) - s0.replace(tzinfo=None)).total_seconds() >= -slack_s
    except (TypeError, ValueError):
        return True


def _is_uncertain(e: Exception) -> bool:
    n = type(e).__name__
    s = str(e).lower()
    return n in ("NetworkException", "Timeout", "ReadTimeout", "ConnectionError", "TimeoutError") or "timed out" in s or "timeout" in s
