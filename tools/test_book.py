#!/usr/bin/env python3
"""Offline unit tests for the tariff engine (Phase 2) and order lifecycle /
buy fills / holds (Phase 3). No network, no spent practice run.

    python tools/test_book.py         # or: pytest tools/test_book.py
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from book import (State, bps, charges, money, qty_str, route)  # noqa: E402


# -- helpers -----------------------------------------------------------------
def _ev(eid, etype, **payload):
    return {"offset": 0, "event_id": eid, "type": etype, "payload": payload}


def _bal(legs):
    return (sum(D(l["debit"]) for l in legs), sum(D(l["credit"]) for l in legs))


def _amt(legs, account, side):
    return sum(D(l[side]) for l in legs if l["account"] == account)


# -- Phase 2: numeric + tariff ----------------------------------------------
def test_money_half_away_from_zero():
    assert money(D("1.845")) == D("1.85")          # not banker's 1.84
    assert money(D("2.675")) == D("2.68")
    assert bps(D("5000.00"), 20) == D("10.00")


def test_charges_profitable_buy():
    # Worked example A: BRK-A equity, P=5000, rate=0.30
    ch = charges("BRK-A", D("5000.00"), D("0.30"))
    assert ch == {"b": D("10.00"), "c": D("2.00"), "r": D("4.00"),
                  "bc": D("4.85"), "cc": D("1.00"), "ps": D("1.85")}


def test_charges_min_fee_floor_ticket_and_no_clawback():
    # Worked example B: BRK-B, P=200, rate=0.50 -> loss-making, ps clamps to 0
    ch = charges("BRK-B", D("200.00"), D("0.50"))
    assert ch["b"] == D("2.50")        # floored at min fee (raw brokerage 0.30)
    assert ch["bc"] == D("3.16")       # broker cost carries the 3.00 ticket
    assert ch["ps"] == D("0.00")       # negative margin -> no clawback


def test_charges_half_cent_partner_rounding():
    # 6.15 margin * 0.30 = 1.845 -> rounds up to 1.85 (half away from zero)
    assert charges("BRK-A", D("5000.00"), D("0.30"))["ps"] == D("1.85")


def test_route_min_charge_per_class():
    # equity: BRK-A 24bps vs BRK-B 20bps -> BRK-B; etf: BRK-A 24 vs BRK-C 28
    # -> BRK-A; bond: BRK-B 20 vs BRK-C 28 -> BRK-B
    assert route("equity", D("10000")) == "BRK-B"
    assert route("etf", D("10000")) == "BRK-A"
    assert route("bond", D("10000")) == "BRK-B"


def test_route_floor_driven_and_tie_break():
    # tiny notional: both equity brokers hit their min-fee floor. BRK-A floor
    # 1.00 < BRK-B floor 2.50, so BRK-A. (Ties resolve on broker id ascending.)
    assert route("equity", D("1.00")) == "BRK-A"


# -- Phase 3: order lifecycle, buy legs, holds -------------------------------
def _place_buy(st, oid="O1", cid="C1", sym="ACME", qty="100",
               limit="50", cls="equity", est="20.00"):
    st.apply(_ev("p_" + oid, "order_placed", order_id=oid, customer_id=cid,
                 side="buy", symbol=sym, quantity=D(qty), limit_price=D(limit),
                 asset_class=cls, est_charges=D(est)))


def _fill(st, oid="O1", cid="C1", sym="ACME", qty="100", price="50",
          principal="5000.00", cls="equity", broker="BRK-A", rate="0.30",
          tid="T1", final=True):
    etype = "order_filled" if final else "order_partially_filled"
    return st.apply(_ev("f_" + tid, etype, order_id=oid, customer_id=cid,
                        side="buy", symbol=sym, quantity=D(qty), price=D(price),
                        principal=D(principal), asset_class=cls, broker=broker,
                        partner_rate=D(rate), trade_id=tid))


def test_buy_fill_balances_and_firm_accounts():
    st = State()
    _place_buy(st)
    legs = _fill(st)
    dr, cr = _bal(legs)
    assert dr == cr == D("10023.70")
    # firm-accounts block (all-or-nothing): each posted exactly once
    assert _amt(legs, "4000", "credit") == D("10.00")   # brokerage revenue
    assert _amt(legs, "4010", "credit") == D("2.00")    # custody revenue
    assert _amt(legs, "2400", "credit") == D("4.00")    # reg owed onward
    assert _amt(legs, "2411", "credit") == D("4.85")    # BRK-A payable
    assert _amt(legs, "2420", "credit") == D("1.00")    # custodian payable
    assert _amt(legs, "2430", "credit") == D("1.85")    # partner payable
    assert _amt(legs, "5000", "debit") == D("4.85")
    assert _amt(legs, "5010", "debit") == D("1.00")
    assert _amt(legs, "5100", "debit") == D("1.85")
    # customer side and the receivable/custody legs
    assert _amt(legs, "2010", "debit") == D("5016.00")  # P + b + c + r
    assert _amt(legs, "2350", "credit") == D("5000.00")
    assert _amt(legs, "1200", "debit") == D("5000.00")
    assert _amt(legs, "2100", "credit") == D("5000.00")


def test_buy_fill_adds_lot_and_records_trade():
    st = State()
    _place_buy(st)
    _fill(st)
    lots = st.lots[("C1", "ACME")]
    assert len(lots) == 1 and lots[0].qty == D("100") and lots[0].cost == D("5000.00")
    assert st.trades["T1"] == {"side": "buy", "principal": D("5000.00"),
                               "customer_id": "C1", "settled": False}


def test_hold_zero_after_close_and_positions_reported():
    st = State()
    _place_buy(st)
    _fill(st)                                  # order_filled closes it
    snap = st.snapshot()
    assert snap["customers"]["C1"]["cash_hold"] == "0.00"
    assert snap["customers"]["C1"]["positions"] == {
        "ACME": {"quantity": "100", "cost_basis": "5000.00"}}
    assert snap["open_order_routes"] == {}     # closed -> not reported


def test_partial_fill_releases_proportional_hold_and_reports_route():
    st = State()
    _place_buy(st)                             # hold = 100*50 + 20 = 5020.00
    _fill(st, qty="40", principal="2000.00", tid="T1", final=False)
    snap = st.snapshot()
    # released 5020 * 40/100 = 2008.00 -> remaining 3012.00
    assert snap["customers"]["C1"]["cash_hold"] == "3012.00"
    # still open -> reports our computed route for equity (BRK-B, cheapest)
    assert snap["open_order_routes"] == {"O1": "BRK-B"}


def test_sell_fill_deferred_to_phase4_not_crash():
    st = State()
    legs = st.apply(_ev("f_s1", "order_filled", order_id="S1", customer_id="C1",
                        side="sell", symbol="ACME", quantity=D("10"),
                        price=D("60"), principal=D("600.00"), asset_class="equity",
                        broker="BRK-A", partner_rate=D("0.30"), trade_id="S1"))
    assert legs == []                          # no wrong legs posted
    assert st.todo["order_filled(sell)"] == 1
    assert st.orders["S1"]["closed"] is True   # lifecycle still advanced


def test_fill_before_placement_is_handled():
    st = State()
    legs = _fill(st, oid="O9", tid="T9")       # fill with no prior placement
    dr, cr = _bal(legs)
    assert dr == cr == D("10023.70")           # legs still post correctly
    assert st.orders["O9"]["closed"] is True


def test_cancel_releases_hold_no_legs():
    st = State()
    _place_buy(st)
    legs = st.apply(_ev("c1", "order_cancelled", order_id="O1"))
    assert legs == []
    assert st.snapshot()["customers"].get("C1", {}).get("cash_hold", "0.00") == "0.00"
    assert st.snapshot()["open_order_routes"] == {}


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
