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


def _sell(st, oid="O1", cid="C1", sym="ACME", qty="100", price="60",
          principal="6000.00", cls="equity", broker="BRK-A", rate="0.30",
          tid="S1", final=True):
    etype = "order_filled" if final else "order_partially_filled"
    return st.apply(_ev("s_" + tid, etype, order_id=oid, customer_id=cid,
                        side="sell", symbol=sym, quantity=D(qty), price=D(price),
                        principal=D(principal), asset_class=cls, broker=broker,
                        partner_rate=D(rate), trade_id=tid))


def test_sell_fill_balances_fifo_cost_and_net_credit():
    # Worked example C: buy 100 @ cost 5000, sell 100 P=6000 BRK-A rate 0.30.
    st = State()
    _place_buy(st)
    _fill(st)                                   # lot: qty 100, cost 5000.00
    legs = _sell(st, oid="O2", tid="S1")
    dr, cr = _bal(legs)
    assert dr == cr == D("11009.19")
    assert _amt(legs, "1150", "debit") == D("6000.00")     # receivable, not 2350
    assert _amt(legs, "2010", "credit") == D("5980.80")    # P - b - c - r
    assert _amt(legs, "2100", "debit") == D("5000.00")     # claim shrinks by cost k
    assert _amt(legs, "1200", "credit") == D("5000.00")
    assert st.lots.get(("C1", "ACME"), []) == []           # position fully closed
    assert st.trades["S1"]["side"] == "sell"


def test_fifo_partial_lot_relief_total_cost_based():
    st = State()
    _place_buy(st)
    _fill(st)                                   # lot: qty 100, cost 5000.00
    _sell(st, oid="O2", qty="40", principal="2400.00", tid="S1")
    lots = st.lots[("C1", "ACME")]
    # relief = round(5000 * 40/100) = 2000.00; remainder stays with the lot
    assert len(lots) == 1 and lots[0].qty == D("60") and lots[0].cost == D("3000.00")
    snap = st.snapshot()
    assert snap["customers"]["C1"]["positions"]["ACME"] == {
        "quantity": "60", "cost_basis": "3000.00"}


def test_fifo_spans_multiple_lots_oldest_first():
    st = State()
    # two lots at different cost-per-share: (10 @ total 100), (10 @ total 300)
    _fill(st, oid="Oa", sym="XYZ", qty="10", price="10", principal="100.00", tid="Ta")
    _fill(st, oid="Ob", sym="XYZ", qty="10", price="30", principal="300.00", tid="Tb")
    # sell 15: consume lot1 fully (100) + 5/10 of lot2 -> round(300*5/10)=150
    legs = _sell(st, oid="Os", sym="XYZ", qty="15", price="40",
                 principal="600.00", tid="Ts")
    assert _amt(legs, "2100", "debit") == D("250.00")      # 100 + 150 = k
    lots = st.lots[("C1", "XYZ")]
    assert len(lots) == 1 and lots[0].qty == D("5") and lots[0].cost == D("150.00")


def test_oversell_rejected_with_zero_mutation():
    st = State()
    _fill(st, oid="Ob", sym="ACME", qty="10", price="50", principal="500.00", tid="Tb")
    before = [(l.qty, l.cost) for l in st.lots[("C1", "ACME")]]
    legs = _sell(st, oid="Os", sym="ACME", qty="20", principal="1200.00", tid="Ts")
    assert legs == []                                       # refused
    after = [(l.qty, l.cost) for l in st.lots[("C1", "ACME")]]
    assert before == after                                 # lot book untouched
    assert "Ts" not in st.trades
    assert "Os" not in st.orders                            # no lifecycle trace


# -- Phase 1: cash events ----------------------------------------------------
def test_fee_charged_then_refund_and_guards():
    st = State()
    st.apply(_ev("d1", "deposit", customer_id="C1", amount=D("100.00")))
    fee = st.apply(_ev("fee1", "fee_charged", customer_id="C1", amount=D("5.00")))
    assert _amt(fee, "2010", "debit") == D("5.00") and _amt(fee, "1100", "credit") == D("5.00")
    ref = st.apply(_ev("r1", "fee_refund", refunds_source_id="fee1", customer_id="C1"))
    assert _amt(ref, "1100", "debit") == D("5.00") and _amt(ref, "2010", "credit") == D("5.00")
    # wallet back to the post-deposit 100.00
    assert st.snapshot()["customers"]["C1"]["wallet_cash"] == "100.00"
    # double refund and unknown reference both reject
    assert st.apply(_ev("r2", "fee_refund", refunds_source_id="fee1", customer_id="C1")) == []
    assert st.apply(_ev("r3", "fee_refund", refunds_source_id="nope", customer_id="C1")) == []


def test_withdrawal_lifecycle():
    st = State()
    st.apply(_ev("d1", "deposit", customer_id="C1", amount=D("100.00")))
    req = st.apply(_ev("w1", "withdrawal_requested", withdrawal_id="W1",
                       customer_id="C1", amount=D("30.00")))
    assert _amt(req, "2010", "debit") == D("30.00") and _amt(req, "2300", "credit") == D("30.00")
    # settle looks the amount up from the request
    sett = st.apply(_ev("w2", "withdrawal_settled", withdrawal_id="W1"))
    assert _amt(sett, "2300", "debit") == D("30.00") and _amt(sett, "1100", "credit") == D("30.00")
    # a second settle (already closed) and an unknown id both reject
    assert st.apply(_ev("w3", "withdrawal_settled", withdrawal_id="W1")) == []
    assert st.apply(_ev("w4", "withdrawal_rejected", withdrawal_id="ghost")) == []


def test_withdrawal_rejected_returns_to_wallet():
    st = State()
    st.apply(_ev("w1", "withdrawal_requested", withdrawal_id="W1",
                 customer_id="C1", amount=D("30.00")))
    legs = st.apply(_ev("w2", "withdrawal_rejected", withdrawal_id="W1"))
    assert _amt(legs, "2300", "debit") == D("30.00") and _amt(legs, "2010", "credit") == D("30.00")


def test_interest_credited_split_and_guard():
    st = State()
    legs = st.apply(_ev("i1", "interest_credited", customer_id="C1",
                        gross_amount=D("10.00"), customer_share=D("7.00")))
    assert _amt(legs, "1100", "debit") == D("10.00")
    assert _amt(legs, "2010", "credit") == D("7.00")
    assert _amt(legs, "4200", "credit") == D("3.00")       # firm keeps the rest
    # gross == share -> the zero 4200 leg is omitted
    legs2 = st.apply(_ev("i2", "interest_credited", customer_id="C1",
                         gross_amount=D("5.00"), customer_share=D("5.00")))
    assert all(l["account"] != "4200" for l in legs2)
    # share > gross is bad data -> rejected
    assert st.apply(_ev("i3", "interest_credited", customer_id="C1",
                        gross_amount=D("5.00"), customer_share=D("6.00"))) == []


def test_transfer_between_customers_account_nets_zero():
    st = State()
    st.apply(_ev("d1", "deposit", customer_id="C1", amount=D("100.00")))
    legs = st.apply(_ev("t1", "transfer_between_customers", from_customer_id="C1",
                        to_customer_id="C2", amount=D("30.00")))
    assert _amt(legs, "2010", "debit") == D("30.00") and _amt(legs, "2010", "credit") == D("30.00")
    snap = st.snapshot()
    assert snap["customers"]["C1"]["wallet_cash"] == "70.00"
    assert snap["customers"]["C2"]["wallet_cash"] == "30.00"
    assert snap["trial_balance"]["2010"] == "-100.00"      # account nets across customers
    # transfer to self is refused
    assert st.apply(_ev("t2", "transfer_between_customers", from_customer_id="C1",
                        to_customer_id="C1", amount=D("10.00"))) == []


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


# -- Phase 5: trade settlement -----------------------------------------------
def test_trade_settled_buy_then_sell():
    st = State()
    _place_buy(st)
    _fill(st)                                   # buy trade T1, principal 5000
    legs = st.apply(_ev("ts1", "trade_settled", trade_id="T1"))
    assert _amt(legs, "2350", "debit") == D("5000.00")     # obligation discharged
    assert _amt(legs, "1100", "credit") == D("5000.00")    # cash finally leaves
    assert st.balances[("C1", "2350")] == D("0.00")
    _sell(st, oid="O2", tid="S1")                          # sell trade S1, P 6000
    legs2 = st.apply(_ev("ts2", "trade_settled", trade_id="S1"))
    assert _amt(legs2, "1100", "debit") == D("6000.00")    # cash arrives
    assert _amt(legs2, "1150", "credit") == D("6000.00")   # receivable cleared
    assert st.balances[("C1", "1150")] == D("0.00")
    assert st.trades["S1"]["settled"] is True


def test_trade_settled_unknown_and_double_reject():
    st = State()
    _place_buy(st)
    _fill(st)
    assert st.apply(_ev("tsx", "trade_settled", trade_id="NOPE")) == []
    st.apply(_ev("ts1", "trade_settled", trade_id="T1"))
    assert st.apply(_ev("ts2", "trade_settled", trade_id="T1")) == []   # already done


# -- Phase 5b: paying accrued payables onward --------------------------------
def test_payable_settlements_clear_firm_accounts():
    st = State()
    _place_buy(st)
    _fill(st)                                   # accrues 2411/2420/2400/2430
    # BRK-A P=5000 rate .30 -> bc 4.85, cc 1.00, r 4.00, ps 1.85
    b1 = st.apply(_ev("bf", "broker_fees_settled", customer_id="C1", broker="BRK-A"))
    assert _amt(b1, "2411", "debit") == D("4.85") and _amt(b1, "1100", "credit") == D("4.85")
    c1 = st.apply(_ev("cf", "custodian_fees_settled", customer_id="C1"))
    assert _amt(c1, "2420", "debit") == D("1.00")
    r1 = st.apply(_ev("rf", "reg_fees_remitted", customer_id="C1"))
    assert _amt(r1, "2400", "debit") == D("4.00")
    p1 = st.apply(_ev("pp", "partner_payout", customer_id="C1"))
    assert _amt(p1, "2430", "debit") == D("1.85")
    # every payable is now flat for this customer
    for acct in ("2411", "2420", "2400", "2430"):
        assert st.balances.get(("C1", acct), D("0.00")) == D("0.00"), acct


def test_settle_payable_with_nothing_outstanding_rejected():
    st = State()
    _place_buy(st)
    _fill(st)
    st.apply(_ev("pp1", "partner_payout", customer_id="C1"))      # clears 2430
    # a second payout has nothing accrued -> reject, and no spurious account
    assert st.apply(_ev("pp2", "partner_payout", customer_id="C1")) == []
    assert st.apply(_ev("cf0", "custodian_fees_settled", customer_id="NOBODY")) == []
    assert ("NOBODY", "2420") not in st.balances                  # .get, not [], on reject


def test_settlement_amount_audits_accumulated_rounding():
    # two fills accrue reg fees independently-rounded; the remit pays the sum
    st = State()
    _fill(st, oid="Oa", qty="10", price="33.33", principal="333.30", tid="Ta")  # r=bps(333.30,8)=0.27
    _fill(st, oid="Ob", qty="10", price="77.77", principal="777.70", tid="Tb")  # r=bps(777.70,8)=0.62
    legs = st.apply(_ev("rr", "reg_fees_remitted", customer_id="C1"))
    assert _amt(legs, "2400", "debit") == D("0.89")              # 0.27 + 0.62


# -- Phase 6: as-of checkpoints ----------------------------------------------
def test_asof_checkpoint_reflects_history_not_current():
    from book import Book
    b = Book(dump=None)
    b.apply(_ev("d1", "deposit", customer_id="C1", amount=D("100.00")))
    b.apply(_ev("p1", "order_placed", order_id="O1", customer_id="C1", side="buy",
                symbol="ACME", quantity=D("10"), limit_price=D("10"),
                asset_class="equity", est_charges=D("2.00")))
    b.apply(_ev("f1", "order_filled", order_id="O1", customer_id="C1", side="buy",
                symbol="ACME", quantity=D("10"), price=D("10"),
                principal=D("100.00"), asset_class="equity", broker="BRK-A",
                partner_rate=D("0.30"), trade_id="T1"))
    # as-of the deposit: no position and no order yet
    early = b.snapshot(as_of="d1")
    assert early["customers"]["C1"]["positions"] == {}
    assert early["open_order_routes"] == {}
    # as-of the placement: order open, hold reported, still no position.
    # notional 100 -> min-fee floors dominate, so BRK-A (1.04) beats BRK-B (2.55).
    placed = b.snapshot(as_of="p1")
    assert placed["open_order_routes"] == {"O1": "BRK-A"}
    assert placed["customers"]["C1"]["cash_hold"] == "102.00"
    # current: filled -> position exists, order closed, hold released
    cur = b.snapshot()
    assert cur["customers"]["C1"]["positions"]["ACME"]["quantity"] == "10"
    assert cur["open_order_routes"] == {}


# -- Phase 7: corporate actions ----------------------------------------------
def test_dividend_cash_net_only():
    st = State()
    legs = st.apply(_ev("dv1", "dividend_cash", customer_id="C1", symbol="ACME",
                        gross_amount=D("100.00"), withholding_tax=D("15.00"),
                        net_amount=D("85.00")))
    assert _amt(legs, "1100", "debit") == D("85.00")
    assert _amt(legs, "2010", "credit") == D("85.00")       # no tax payable leg


def test_dividend_reinvested_adds_lot_no_cash():
    st = State()
    legs = st.apply(_ev("dv2", "dividend_reinvested", customer_id="C1", symbol="ACME",
                        gross_amount=D("100.00"), withholding_tax=D("15.00"),
                        net_amount=D("85.00"), reinvest_price=D("8.50"),
                        reinvest_quantity=D("10")))
    assert _amt(legs, "1200", "debit") == D("85.00")
    assert _amt(legs, "2100", "credit") == D("85.00")       # cash never involved
    lots = st.lots[("C1", "ACME")]
    assert lots[0].qty == D("10") and lots[0].cost == D("85.00")


def test_stock_split_scales_qty_keeps_total_cost():
    st = State()
    _place_buy(st)
    _fill(st)                                    # lot 100 @ cost 5000.00
    legs = st.apply(_ev("sp1", "stock_split", customer_id="C1", symbol="ACME",
                        ratio_from=D("1"), ratio_to=D("2")))    # 2-for-1
    assert legs == []
    snap = st.snapshot()["customers"]["C1"]["positions"]["ACME"]
    assert snap == {"quantity": "200", "cost_basis": "5000.00"}  # cost unchanged


def test_symbol_change_rekeys_and_preserves_cost():
    st = State()
    _place_buy(st)
    _fill(st)                                    # ACME lot 100 @ 5000
    st.apply(_ev("sc1", "symbol_change", customer_id="C1",
                 old_symbol="ACME", new_symbol="BCME"))
    pos = st.snapshot()["customers"]["C1"]["positions"]
    assert "ACME" not in pos
    assert pos["BCME"] == {"quantity": "100", "cost_basis": "5000.00"}


def test_symbol_change_into_occupied_merges_fifo():
    st = State()
    # buy BCME first (older, seq 1), then ACME (newer, seq 2)
    _fill(st, oid="Ob", sym="BCME", qty="10", price="10", principal="100.00", tid="Tb")
    _fill(st, oid="Oa", sym="ACME", qty="10", price="50", principal="500.00", tid="Ta")
    st.apply(_ev("sc1", "symbol_change", customer_id="C1",
                 old_symbol="ACME", new_symbol="BCME"))     # merge into BCME
    # sell 10: FIFO must take the older BCME lot (cost 100) first, not ACME's 500
    legs = _sell(st, oid="Os", sym="BCME", qty="10", price="20",
                 principal="200.00", tid="Ts")
    assert _amt(legs, "2100", "debit") == D("100.00")       # oldest lot relieved


# -- Phase 8: reversals ------------------------------------------------------
def test_reversal_of_buy_inverts_legs_and_removes_lot():
    st = State()
    _place_buy(st)
    _fill(st)                                    # buy event id "f_T1"
    rev = st.apply(_ev("rev1", "reversal", reverses_event_id="f_T1", reason="oops"))
    assert _amt(rev, "2010", "credit") == D("5016.00")      # inverse of Dr 2010
    assert _amt(rev, "2350", "debit") == D("5000.00")       # inverse of Cr 2350
    assert st.lots.get(("C1", "ACME"), []) == []            # lot removed
    assert st.snapshot()["customers"]["C1"]["positions"] == {}   # as if never bought
    assert money(sum(st.balances.values())) == D("0.00")


def test_reversal_of_sell_restores_consumed_lot():
    st = State()
    _place_buy(st)
    _fill(st)                                    # lot 100 @ 5000
    _sell(st, oid="O2", qty="40", principal="2400.00", tid="S1")  # -> lot 60 @ 3000
    st.apply(_ev("rev1", "reversal", reverses_event_id="s_S1"))
    lots = st.lots[("C1", "ACME")]
    assert sum(l.qty for l in lots) == D("100")
    assert sum(l.cost for l in lots) == D("5000.00")        # cost fully restored


def test_reversal_of_split_scales_back():
    st = State()
    _place_buy(st)
    _fill(st)
    st.apply(_ev("sp1", "stock_split", customer_id="C1", symbol="ACME",
                 ratio_from=D("1"), ratio_to=D("2")))
    st.apply(_ev("rev1", "reversal", reverses_event_id="sp1"))
    lots = st.lots[("C1", "ACME")]
    assert sum(l.qty for l in lots) == D("100") and sum(l.cost for l in lots) == D("5000.00")


def test_reversal_unknown_ref_rejected():
    st = State()
    assert st.apply(_ev("revx", "reversal", reverses_event_id="ghost")) == []


def test_reversal_of_fill_does_not_restore_hold():
    st = State()
    _place_buy(st)                               # hold 5020.00
    _fill(st, qty="40", principal="2000.00", tid="T1", final=False)  # remaining 3012
    st.apply(_ev("rev1", "reversal", reverses_event_id="f_T1"))
    snap = st.snapshot()["customers"]["C1"]
    assert snap["cash_hold"] == "3012.00"        # released hold stays released
    assert snap["positions"] == {}               # but the lot is gone


# -- Phase 9: fx_deposit -----------------------------------------------------
def test_fx_deposit_spread_to_4100():
    st = State()
    legs = st.apply(_ev("fx1", "fx_deposit", customer_id="C1", amount_foreign=D("1000"),
                        currency="EUR", market_rate=D("1.10"), customer_rate=D("1.08"),
                        usd_at_market_rate=D("1100.00"), usd_at_customer_rate=D("1080.00")))
    assert _amt(legs, "1100", "debit") == D("1100.00")
    assert _amt(legs, "2010", "credit") == D("1080.00")
    assert _amt(legs, "4100", "credit") == D("20.00")       # firm's FX spread


def test_fx_deposit_negative_spread_rejected():
    st = State()
    assert st.apply(_ev("fx2", "fx_deposit", customer_id="C1", amount_foreign=D("1000"),
                        currency="EUR", market_rate=D("1.08"), customer_rate=D("1.10"),
                        usd_at_market_rate=D("1080.00"),
                        usd_at_customer_rate=D("1100.00"))) == []


# -- Phase 10: resilience & defect audit -------------------------------------
def test_audit_flags_inconsistent_fills():
    st = State()
    # principal disagrees with qty*price
    st.apply(_ev("f1", "order_filled", order_id="O1", customer_id="C1", side="buy",
                 symbol="ACME", quantity=D("10"), price=D("50"), principal=D("999.00"),
                 asset_class="equity", broker="BRK-A", partner_rate=D("0.30"), trade_id="T1"))
    assert st.stats["audit:principal!=qty*price"] == 1
    # BRK-A does not trade bonds
    st.apply(_ev("f2", "order_filled", order_id="O2", customer_id="C1", side="buy",
                 symbol="BND", quantity=D("10"), price=D("50"), principal=D("500.00"),
                 asset_class="bond", broker="BRK-A", partner_rate=D("0.30"), trade_id="T2"))
    assert st.stats["audit:broker!=asset_class"] == 1


def test_nonstrict_never_stalls_but_strict_surfaces():
    bad = _ev("f1", "order_filled", order_id="O1", customer_id="C1", side="buy",
              symbol="ACME", quantity=D("10"), price=D("50"), principal=D("500.00"),
              asset_class="equity", partner_rate=D("0.30"), trade_id="T1")  # no broker
    lenient = State(strict=False)
    assert lenient.apply(bad) == []                         # rejected, not raised
    assert lenient.stats["error:order_filled"] == 1
    raised = False
    try:
        State(strict=True).apply(dict(bad))
    except Exception:
        raised = True
    assert raised                                           # our bug would surface


def test_reconcile_clean_after_balanced_run():
    st = State()
    _place_buy(st)
    _fill(st)
    r = st.reconcile()
    assert r["trial_balance_zero"] and not r["negative_positions"]
    assert not r["negative_lot_cost"] and not r["negative_holds"]
    assert r["unsettled_trades"] == 1                       # the buy trade pending


def test_idempotent_across_forced_reset():
    from book import Book
    b = Book(dump=None)
    evs = [
        _ev("d1", "deposit", customer_id="C1", amount=D("1000.00")),
        _ev("p1", "order_placed", order_id="O1", customer_id="C1", side="buy",
            symbol="ACME", quantity=D("10"), limit_price=D("50"),
            asset_class="equity", est_charges=D("5.00")),
        _ev("f1", "order_filled", order_id="O1", customer_id="C1", side="buy",
            symbol="ACME", quantity=D("10"), price=D("50"), principal=D("500.00"),
            asset_class="equity", broker="BRK-A", partner_rate=D("0.30"), trade_id="T1"),
    ]
    for e in evs:
        b.apply(e)
    snap1 = b.snapshot()
    for e in evs:                                            # server re-delivers them
        b.apply(e)
    assert b.snapshot() == snap1                             # byte-identical


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
