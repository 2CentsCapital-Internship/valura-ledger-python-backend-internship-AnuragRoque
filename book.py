"""Your ledger. This is the whole assignment.

`client.py` handles the network and hands you one event at a time. You return
the journal legs it produced. Some events correctly produce none: return an
empty list, not None-as-an-accident.

Design (see plan.md): the book is a fold over an ordered log of first-delivery
events.

  * ``State`` is the whole ledger and the one place events are interpreted. Its
    ``apply(ev) -> legs`` is a *pure* function of prior state + event: compute
    legs, validate, then mutate. No clock, I/O or randomness, so replaying the
    log reproduces the book exactly.
  * ``Book`` is the thin wrapper the client calls. It owns idempotency (the
    seen-set), the delivery-order log, and snapshots (current, or a from-scratch
    prefix replay for an as-of checkpoint).

Two things graded exactly, locked in here:

  * Money is ``Decimal``, never ``float`` -- and a float must never reach a
    handler. We do not edit the transport, so every JSON number is converted to
    ``Decimal`` at the single ingress point (``Book.apply``) before any handler
    sees it. Both the live run and the offline replay go through that same
    point, so they cannot disagree on a value.
  * Balances are keyed by ``(customer_id, account)``, never by account alone.
    ``transfer_between_customers`` moves money between two customers on the same
    account 2010; an account-level book shows nothing wrong and fails every
    later checkpoint.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP

D = Decimal
ZERO = D("0.00")


# ---------------------------------------------------------------------------
# Numeric helpers (the silent score-killers -- see plan.md section 4)
# ---------------------------------------------------------------------------
def money(x) -> Decimal:
    """2 dp, half away from zero. Not round(), which is banker's (half-even)."""
    return D(x).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def bps(principal, n) -> Decimal:
    """``n`` basis points of ``principal``, rounded to the cent independently.

    Every derived charge is rounded on its own before use; this is the only
    place that rounding happens for a tariff component.
    """
    return money(D(principal) * D(n) / D(10000))


def qty6(q) -> Decimal:
    """A share quantity to 6 dp, half away from zero. Splits can produce a
    non-terminating ratio; quantities are graded to at most 6 dp.
    """
    return D(q).quantize(D("0.000001"), rounding=ROUND_HALF_UP)


def qty_str(q) -> str:
    """A share quantity as a plain decimal string: ``"8"``, never ``"8.000000"``
    and never ``"1E+1"``.

    ``normalize()`` trims trailing zeros (and can hand back scientific form like
    ``1E+1``); format ``'f'`` forces plain fixed-point, so ``10`` prints ``10``.
    """
    return format(D(q).normalize(), "f")


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    return {"account": account, "customer_id": customer_id,
            "debit": str(money(debit)), "credit": str(money(credit))}


# Whether a fill's zero-value legs (e.g. a loss-making fill's zero partner share,
# ~1/4 of fills) are dropped. Omitting is balance-safe and leaves firm-account
# balances identical either way; only per-event leg matching differs. The first
# practice buy fill settles it -- practice returns the worked examples' full legs
# -- so this is one switch to flip, not a scattered decision. [plan.md section 14]
OMIT_ZERO_LEGS = True


def _nonzero(legs: list[dict]) -> list[dict]:
    """Drop legs that are 0.00 on both sides, per OMIT_ZERO_LEGS."""
    if not OMIT_ZERO_LEGS:
        return legs
    return [l for l in legs if l["debit"] != "0.00" or l["credit"] != "0.00"]


# ---------------------------------------------------------------------------
# Reference tables (the live set -- notes2.txt sections 2 and 4)
# ---------------------------------------------------------------------------
# code -> (name, type). Assets rise on debit, liabilities/income/expense on
# credit; the trial balance is debit-positive.
ACCOUNTS: dict[str, tuple[str, str]] = {
    "1100": ("Omnibus Cash at Broker", "Asset"),
    "1150": ("Settlement Receivable", "Asset"),
    "1200": ("Omnibus Custody", "Asset"),
    "2010": ("Customer Wallet", "Liability"),
    "2100": ("Customer Securities Claim", "Liability"),
    "2300": ("Withdrawals In Transit", "Liability"),
    "2350": ("Unsettled Trade Payable", "Liability"),
    "2400": ("Regulatory Fees Payable", "Liability"),
    "2411": ("Broker Fees Payable - BRK-A", "Liability"),
    "2412": ("Broker Fees Payable - BRK-B", "Liability"),
    "2413": ("Broker Fees Payable - BRK-C", "Liability"),
    "2420": ("Custodian Fees Payable", "Liability"),
    "2430": ("Partner Share Payable", "Liability"),
    "4000": ("Brokerage Revenue", "Income"),
    "4010": ("Custody Revenue", "Income"),
    "4100": ("FX Spread Revenue", "Income"),
    "4200": ("Interest Income", "Income"),
    "5000": ("Brokerage Cost", "Expense"),
    "5010": ("Custody Cost", "Expense"),
    "5100": ("Partner Revenue Share", "Expense"),
}

# Every symbol is one asset class for the whole run. All bps are per unit of
# principal. Brokerage is floored at ``min_fee``; every fill also costs the flat
# ``ticket``. ``payable`` is the 241x broker-fees account.
BROKERS: dict[str, dict] = {
    "BRK-A": {"classes": {"equity", "etf"}, "brokerage": 20, "custody": 4,
              "broker_cost": 9, "custody_cost": 2,
              "min_fee": D("1.00"), "ticket": D("0.35"), "payable": "2411"},
    "BRK-B": {"classes": {"equity", "bond"}, "brokerage": 15, "custody": 5,
              "broker_cost": 8, "custody_cost": 3,
              "min_fee": D("2.50"), "ticket": D("3.00"), "payable": "2412"},
    "BRK-C": {"classes": {"etf", "bond"}, "brokerage": 25, "custody": 3,
              "broker_cost": 12, "custody_cost": 1,
              "min_fee": D("0.50"), "ticket": D("0.20"), "payable": "2413"},
}

REG_BPS = 8  # regulatory fee, charged to the customer and owed onward


# ---------------------------------------------------------------------------
# The tariff engine (pure -- see plan.md section 5). Every component is rounded
# to the cent on its own before use; the partner share is then rounded once
# more, off the already-rounded pieces.
# ---------------------------------------------------------------------------
def charges(broker: str, principal, partner_rate) -> dict:
    """The six per-fill amounts for a fill of ``principal`` at ``broker``.

    ``b`` brokerage revenue (Cr 4000), ``c`` custody revenue (Cr 4010),
    ``r`` regulatory owed onward (Cr 2400), ``bc`` broker cost (Dr 5000 / Cr
    241x), ``cc`` custody cost (Dr 5010 / Cr 2420), ``ps`` partner share
    (Dr 5100 / Cr 2430). Brokerage is floored at the min fee; broker cost carries
    the flat ticket; the partner share is ``rate x max(0, revenue - cost)`` with
    no clawback.
    """
    spec = BROKERS[broker]
    P = D(principal)
    b = max(bps(P, spec["brokerage"]), spec["min_fee"])   # floored at min fee
    c = bps(P, spec["custody"])
    r = bps(P, REG_BPS)
    bc = bps(P, spec["broker_cost"]) + spec["ticket"]     # cost carries the ticket
    cc = bps(P, spec["custody_cost"])
    margin = (b + c) - (bc + cc)                          # revenue - cost
    ps = money(D(partner_rate) * max(ZERO, margin))       # clamp at 0: no clawback
    return {"b": b, "c": c, "r": r, "bc": bc, "cc": cc, "ps": ps}


def route(asset_class: str, notional) -> str | None:
    """The broker an open order routes to: the lowest total customer charge
    (brokerage + custody) on ``notional``, among brokers trading that class,
    ties broken on broker id ascending -- so there is always exactly one answer.
    """
    best: tuple[Decimal, str] | None = None
    for bid in sorted(BROKERS):                            # ascending id = tie-break
        spec = BROKERS[bid]
        if asset_class not in spec["classes"]:
            continue
        charge = max(bps(notional, spec["brokerage"]), spec["min_fee"]) \
            + bps(notional, spec["custody"])
        if best is None or charge < best[0]:
            best = (charge, bid)
    return best[1] if best else None


class Rejected(Exception):
    """Raise from a handler for an event you refuse to post on its own merits.

    An oversell, a reversal of something never received, a payload that will not
    parse. A rejected event produces no legs and must leave the book exactly as
    it was. Rejecting one event and carrying on beats stopping.
    """


class Lot:
    """A FIFO purchase parcel: a total cost against a quantity.

    The graded FIFO formula relieves ``round(cost * sold / qty)`` from the total,
    so we carry the *total* cost, never a cost-per-share. ``seq`` is the delivery
    order the lot was created in, so FIFO survives a rename that merges two
    symbols' lots into one book.
    """
    __slots__ = ("qty", "cost", "seq")

    def __init__(self, qty: Decimal, cost: Decimal, seq: int = 0) -> None:
        self.qty = D(qty)
        self.cost = D(cost)
        self.seq = seq


# ---------------------------------------------------------------------------
# State: the whole ledger, and the one place events are interpreted.
# ---------------------------------------------------------------------------
class State:
    def __init__(self, strict: bool = True) -> None:
        # balances[(customer_id, account)] = debit-positive balance
        self.balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self.lots: dict[tuple[str, str], list[Lot]] = defaultdict(list)
        self.fees: dict[str, dict] = {}            # fee_event_id -> {cid, amount}
        self.refunded: set[str] = set()            # fees already refunded once
        self.withdrawals: dict[str, dict] = {}     # withdrawal_id -> {cid, amount, status}
        self.orders: dict[str, dict] = {}          # order_id -> Order
        self.trades: dict[str, dict] = {}          # trade_id -> {side, principal, ...}
        self.legs_by_id: dict[str, list[dict]] = {}  # event_id -> legs (reversal source)
        self.lot_undo: dict[str, object] = {}      # event_id -> lot-book undo record
        self.reversed: set[str] = set()            # events already reversed
        self.posted_accounts: set[str] = set()     # every account ever touched
        self.todo: dict[str, int] = defaultdict(int)  # unhandled types (skeleton)
        self.stats: Counter = Counter()            # per-type / per-outcome tallies
        self._seq = 0                              # monotonic lot delivery counter
        # strict surfaces our own bugs (tests, offline replay); the live Book
        # runs non-strict so an unexpected fault becomes a reject, never a stall.
        self.strict = strict

    def _new_lot(self, qty, cost) -> Lot:
        self._seq += 1
        return Lot(qty, cost, self._seq)

    # -- the pure entry point ----------------------------------------------
    def apply(self, ev: dict) -> list[dict]:
        """Interpret one event: compute legs, validate, then mutate.

        Validate-first / mutate-last: a handler computes its legs and raises
        ``Rejected`` before touching state, so a refusal leaves the book
        untouched. A malformed payload becomes a rejection here rather than a
        crash -- the most expensive mistake is stopping.
        """
        etype = ev["type"]
        self.stats[etype] += 1
        self._audit(etype, ev.get("payload") or {})   # defect-hunt telemetry
        handler = getattr(self, "on_" + etype, None)
        if handler is None:
            self.todo[etype] += 1
            return []
        try:
            legs = handler(ev["payload"], ev) or []
            self._post(legs)
        except Rejected:
            self.stats["reject:" + etype] += 1
            return []
        except Exception:
            # Never stall the run: a malformed payload or an unexpected fault
            # becomes a rejected event. In strict mode (tests, offline replay)
            # it surfaces instead, so our own bugs are never masked.
            if self.strict:
                raise
            self.stats["error:" + etype] += 1
            return []
        self.legs_by_id[ev["event_id"]] = legs
        self.stats["posted:" + etype] += 1
        return legs

    def _post(self, legs: list[dict]) -> None:
        dr = sum((D(l["debit"]) for l in legs), ZERO)
        cr = sum((D(l["credit"]) for l in legs), ZERO)
        if money(dr) != money(cr):
            # Our own arithmetic is wrong -- surface it, don't mask it as a
            # rejection. (Hardened against stalling the live run in Phase 10.)
            raise AssertionError(f"unbalanced: dr {dr} cr {cr}")
        for l in legs:
            self.balances[(l["customer_id"], l["account"])] += (
                D(l["debit"]) - D(l["credit"]))
            self.posted_accounts.add(l["account"])

    # -- worked example (authoritative) ------------------------------------
    def on_deposit(self, p: dict, ev: dict) -> list[dict]:
        """Cash arrives, and the firm owes the customer more.

            Dr 1100 amount        Cr 2010 amount
        """
        amount = money(p["amount"])
        cid = p["customer_id"]
        return [leg("1100", cid, debit=amount),
                leg("2010", cid, credit=amount)]

    # -- cash events (Phase 1) ---------------------------------------------
    def on_fee_charged(self, p: dict, ev: dict) -> list[dict]:
        """The customer pays the firm's fee from their wallet; cash leaves the
        omnibus account.  Dr 2010 / Cr 1100.
        """
        cid = p["customer_id"]
        amount = money(p["amount"])
        # remember it so a later fee_refund can look the amount up by id
        self.fees[ev["event_id"]] = {"customer_id": cid, "amount": amount}
        return [leg("2010", cid, debit=amount), leg("1100", cid, credit=amount)]

    def on_fee_refund(self, p: dict, ev: dict) -> list[dict]:
        """Undo an earlier fee in full. The amount is not in this payload -- it is
        the amount of the fee_charged named by refunds_source_id. Refunding the
        same fee twice is an error.  Dr 1100 / Cr 2010.
        """
        src = p["refunds_source_id"]
        fee = self.fees.get(src)
        if fee is None:
            raise Rejected("refund of a fee never seen")
        if src in self.refunded:
            raise Rejected("fee already refunded")
        self.refunded.add(src)
        cid = fee["customer_id"]          # the customer the fee concerned
        return [leg("1100", cid, debit=fee["amount"]),
                leg("2010", cid, credit=fee["amount"])]

    def on_withdrawal_requested(self, p: dict, ev: dict) -> list[dict]:
        """Money has left the wallet but not the broker -- a different obligation.
        Dr 2010 / Cr 2300.
        """
        wid = p["withdrawal_id"]
        if wid in self.withdrawals:
            raise Rejected("duplicate withdrawal id")
        cid = p["customer_id"]
        amount = money(p["amount"])
        self.withdrawals[wid] = {"customer_id": cid, "amount": amount,
                                 "status": "requested"}
        return [leg("2010", cid, debit=amount), leg("2300", cid, credit=amount)]

    def on_withdrawal_settled(self, p: dict, ev: dict) -> list[dict]:
        """The cash actually leaves; look the amount up from the request.
        Dr 2300 / Cr 1100.
        """
        w = self._open_withdrawal(p["withdrawal_id"])
        w["status"] = "settled"
        return [leg("2300", w["customer_id"], debit=w["amount"]),
                leg("1100", w["customer_id"], credit=w["amount"])]

    def on_withdrawal_rejected(self, p: dict, ev: dict) -> list[dict]:
        """The withdrawal fails; the money is owed to the wallet again. No cash
        ever moved.  Dr 2300 / Cr 2010.
        """
        w = self._open_withdrawal(p["withdrawal_id"])
        w["status"] = "rejected"
        return [leg("2300", w["customer_id"], debit=w["amount"]),
                leg("2010", w["customer_id"], credit=w["amount"])]

    def _open_withdrawal(self, wid: str) -> dict:
        w = self.withdrawals.get(wid)
        # No amount without the request; an unknown/closed id is refused. [reject
        # vs hold-pending for out-of-order settles -- confirm on practice, §14.]
        if w is None or w["status"] != "requested":
            raise Rejected("settle/reject of an unknown or non-open withdrawal")
        return w

    def on_interest_credited(self, p: dict, ev: dict) -> list[dict]:
        """The broker pays interest on the omnibus balance; the customer gets
        their share, the firm keeps the remainder as income (not a pass-through).
        Dr 1100 gross / Cr 2010 share / Cr 4200 remainder.
        """
        cid = p["customer_id"]
        gross = money(p["gross_amount"])
        share = money(p["customer_share"])
        if share > gross:
            raise Rejected("customer share exceeds gross interest")   # bad data
        return _nonzero([leg("1100", cid, debit=gross),
                         leg("2010", cid, credit=share),
                         leg("4200", cid, credit=gross - share)])

    def on_transfer_between_customers(self, p: dict, ev: dict) -> list[dict]:
        """One customer pays another; no external cash moves and the firm's total
        obligation is unchanged -- only whose money it is changes. Both legs land
        on 2010, so the account nets to zero and only the per-customer key sees it.
        """
        frm, to = p["from_customer_id"], p["to_customer_id"]
        if frm == to:
            raise Rejected("transfer to self")     # no-op move, likely the defect
        amount = money(p["amount"])
        return [leg("2010", frm, debit=amount), leg("2010", to, credit=amount)]

    # -- orders: placement, fills, holds (Phase 3) -------------------------
    def _order(self, oid: str, p: dict) -> dict:
        """Get an order, lazily creating a stub if a fill arrived before its
        placement. Details are patched in whenever the placement shows up.
        """
        o = self.orders.get(oid)
        if o is None:
            o = {"customer_id": p.get("customer_id"), "side": p.get("side"),
                 "symbol": p.get("symbol"), "asset_class": p.get("asset_class"),
                 "qty": None, "limit_price": None, "est_charges": ZERO,
                 "hold": ZERO, "remaining_hold": ZERO,
                 "filled_qty": ZERO, "closed": False, "route": None}
            self.orders[oid] = o
        return o

    def on_order_placed(self, p: dict, ev: dict) -> list[dict]:
        """No legs. A placement moves no money -- it creates a hold (reported at
        checkpoints, never posted) and fixes the route for this open order.
        """
        o = self._order(p["order_id"], p)
        o["customer_id"] = p["customer_id"]
        o["side"] = p["side"]
        o["symbol"] = p["symbol"]
        o["asset_class"] = p["asset_class"]
        o["qty"] = D(p["quantity"])
        o["limit_price"] = D(p["limit_price"])
        o["est_charges"] = money(p["est_charges"])
        notional = o["qty"] * o["limit_price"]
        o["route"] = route(p["asset_class"], notional)
        if p["side"] == "buy":
            # buy hold = principal notional + the est_charges given in the feed
            o["hold"] = money(notional + o["est_charges"])
            self._reprice_hold(o)          # reconcile if fills arrived first
        else:
            o["hold"] = o["remaining_hold"] = ZERO   # sell hold is shares, not cash
        return []

    def _reprice_hold(self, o: dict) -> None:
        """Remaining buy hold after the fills seen so far. A fill releases a
        share of the hold proportional to filled quantity; a closed order holds
        nothing.
        """
        if o["closed"] or o["side"] != "buy" or not o["qty"] or not o["hold"]:
            return
        if o["filled_qty"] >= o["qty"]:
            o["remaining_hold"] = ZERO
        else:
            released = money(o["hold"] * o["filled_qty"] / o["qty"])
            o["remaining_hold"] = o["hold"] - released

    def on_order_partially_filled(self, p: dict, ev: dict) -> list[dict]:
        return self._fill(p, ev, final=False)

    def on_order_filled(self, p: dict, ev: dict) -> list[dict]:
        return self._fill(p, ev, final=True)

    def _fill(self, p: dict, ev: dict, final: bool) -> list[dict]:
        # Legs first: a sell can reject (oversell), and a rejected event must
        # leave the book -- including the order lifecycle -- exactly as it was.
        legs = self._buy_fill(p, ev) if p["side"] == "buy" else self._sell_fill(p, ev)

        # Lifecycle, only after the fill posted: create/patch the order (a fill
        # may arrive before its placement), advance it, and release the hold.
        o = self._order(p["order_id"], p)
        for k in ("customer_id", "side", "symbol", "asset_class"):
            o[k] = o[k] or p.get(k)
        o["filled_qty"] += D(p["quantity"])
        if final:                          # last fill closes the order, hold -> 0
            o["closed"] = True
            o["remaining_hold"] = ZERO
        else:
            self._reprice_hold(o)
        return legs

    def _buy_fill(self, p: dict, ev: dict) -> list[dict]:
        """Buy fill (authoritative template, notes2.txt section 4). Customer pays
        principal + all charges; the firm accrues revenue/cost/reg/partner gross;
        cash does not move (settles two days later).
        """
        cid = p["customer_id"]
        P = money(p["principal"])
        ch = charges(p["broker"], P, p["partner_rate"])
        b, c, r = ch["b"], ch["c"], ch["r"]
        bc, cc, ps = ch["bc"], ch["cc"], ch["ps"]
        payable = BROKERS[p["broker"]]["payable"]
        legs = _nonzero([
            leg("2010", cid, debit=P + b + c + r), leg("2350", cid, credit=P),
            leg("1200", cid, debit=P),             leg("2100", cid, credit=P),
            leg("5000", cid, debit=bc),            leg("4000", cid, credit=b),
            leg("5010", cid, debit=cc),            leg("4010", cid, credit=c),
            leg("5100", cid, debit=ps),            leg("2400", cid, credit=r),
            leg(payable, cid, credit=bc),
            leg("2420", cid, credit=cc),
            leg("2430", cid, credit=ps),
        ])
        # FIFO lot: cost basis is the principal only -- commission is the firm's.
        key = (cid, p["symbol"])
        lot = self._new_lot(D(p["quantity"]), P)
        self.lots[key].append(lot)
        self.lot_undo[ev["event_id"]] = ("add_lot", key, lot)   # for reversal (P8)
        self.trades[p["trade_id"]] = {"side": "buy", "principal": P,
                                      "customer_id": cid, "settled": False}
        return legs

    # -- FIFO lot book & sells (Phase 4 -- the biggest lever) --------------
    def _fifo_consume(self, cid: str, sym: str, qty: Decimal):
        """Relieve FIFO cost for a sale of ``qty`` shares, oldest lot first, to
        the cent. Validates the full quantity is available *before* mutating, so
        an oversell rejects with the lot book untouched.

        Returns ``(relief, slices)`` where ``slices`` records what was taken from
        each lot, for the reversal in Phase 8.
        """
        lots = self.lots.get((cid, sym))
        available = sum((l.qty for l in lots), D(0)) if lots else D(0)
        if qty > available:
            raise Rejected("oversell")             # zero mutation on this path
        relief = ZERO
        slices = []                                # (qty_taken, cost_taken, emptied)
        remaining = qty
        while remaining > 0:
            lot = lots[0]
            take = lot.qty if lot.qty <= remaining else remaining
            # Graded convention: round(total_cost * sold / lot_qty), off the
            # lot's *total* cost -- never a cost-per-share, which disagrees by a
            # cent. The remainder stays with the lot.
            cost_take = money(lot.cost * take / lot.qty)
            relief += cost_take
            seq = lot.seq
            lot.qty -= take
            lot.cost -= cost_take
            remaining -= take
            emptied = lot.qty == 0
            slices.append((seq, take, cost_take, emptied))   # seq: exact restore
            if emptied:
                lots.pop(0)
        return relief, slices

    def _sell_fill(self, p: dict, ev: dict) -> list[dict]:
        """Sell fill (derived -- see plan.md section 6). Proceeds are a receivable
        (1150) owed by the broker until settlement; the customer is credited the
        principal net of their charges; custody and the customer's claim shrink by
        the FIFO *cost* of the shares sold, not their sale value. The firm's six
        fee legs match a buy. Realised P/L is the residual of the legs, never posted.
        """
        cid = p["customer_id"]
        P = money(p["principal"])
        ch = charges(p["broker"], P, p["partner_rate"])
        b, c, r = ch["b"], ch["c"], ch["r"]
        bc, cc, ps = ch["bc"], ch["cc"], ch["ps"]
        # cost relief validates the sale first, so an oversell rejects cleanly
        k, slices = self._fifo_consume(cid, p["symbol"], D(p["quantity"]))
        payable = BROKERS[p["broker"]]["payable"]
        legs = _nonzero([
            leg("1150", cid, debit=P),      leg("2010", cid, credit=P - b - c - r),
            leg("2100", cid, debit=k),      leg("1200", cid, credit=k),
            leg("5000", cid, debit=bc),     leg("4000", cid, credit=b),
            leg("5010", cid, debit=cc),     leg("4010", cid, credit=c),
            leg("5100", cid, debit=ps),     leg("2400", cid, credit=r),
            leg(payable, cid, credit=bc),
            leg("2420", cid, credit=cc),
            leg("2430", cid, credit=ps),
        ])
        self.lot_undo[ev["event_id"]] = ("sell", (cid, p["symbol"]), slices)
        self.trades[p["trade_id"]] = {"side": "sell", "principal": P,
                                      "customer_id": cid, "settled": False}
        return legs

    def on_order_cancelled(self, p: dict, ev: dict) -> list[dict]:
        """No legs. Close the order and release whatever hold remains."""
        o = self.orders.get(p["order_id"])
        if o is not None:
            o["closed"] = True
            o["remaining_hold"] = ZERO
        return []

    def on_order_rejected(self, p: dict, ev: dict) -> list[dict]:
        return self.on_order_cancelled(p, ev)

    # -- settlement (Phase 5) ----------------------------------------------
    def on_trade_settled(self, p: dict, ev: dict) -> list[dict]:
        """Settlement day: the cash from that fill actually moves, discharging the
        obligation the fill created. Nothing else about the trade changes.
        Buy: Dr 2350 / Cr 1100.  Sell: Dr 1100 / Cr 1150.
        """
        t = self.trades.get(p["trade_id"])
        if t is None:
            # No side or principal without the fill. [reject vs hold-pending for
            # an out-of-order settle -- confirm on practice, plan.md section 14.]
            raise Rejected("settlement of an unknown trade")
        if t["settled"]:
            raise Rejected("trade already settled")
        t["settled"] = True
        cid, P = t["customer_id"], t["principal"]
        if t["side"] == "buy":
            return [leg("2350", cid, debit=P), leg("1100", cid, credit=P)]
        return [leg("1100", cid, debit=P), leg("1150", cid, credit=P)]

    # -- paying it all onward: discharge accrued payables (Phase 5) ---------
    def _settle_payable(self, cid: str, account: str) -> list[dict]:
        """Discharge a payable in full for one customer, out of omnibus cash. The
        amount is whatever has accrued on that account for them -- never in the
        payload -- so each settlement audits every per-trade rounding since the
        last one. Settling an account with nothing outstanding is an error.
        Dr <payable> accrued / Cr 1100 accrued.
        """
        # .get, not [], so probing a customer never creates a spurious 0.00 entry
        # that would then be reported as an account we posted to.
        accrued = -self.balances.get((cid, account), ZERO)   # liability: credit-positive
        if accrued <= ZERO:
            raise Rejected("settling a payable with nothing outstanding")
        return [leg(account, cid, debit=accrued), leg("1100", cid, credit=accrued)]

    def on_broker_fees_settled(self, p: dict, ev: dict) -> list[dict]:
        """Pay that broker's accumulated fees for this customer (241x)."""
        return self._settle_payable(p["customer_id"], BROKERS[p["broker"]]["payable"])

    def on_custodian_fees_settled(self, p: dict, ev: dict) -> list[dict]:
        """Pay the custodian's accumulated fees for this customer (2420)."""
        return self._settle_payable(p["customer_id"], "2420")

    def on_reg_fees_remitted(self, p: dict, ev: dict) -> list[dict]:
        """Remit the regulatory fees collected for this customer (2400)."""
        return self._settle_payable(p["customer_id"], "2400")

    def on_partner_payout(self, p: dict, ev: dict) -> list[dict]:
        """Pay the introducing partner's accumulated share for this customer (2430)."""
        return self._settle_payable(p["customer_id"], "2430")

    # -- corporate actions, per named customer only (Phase 7) --------------
    def on_dividend_cash(self, p: dict, ev: dict) -> list[dict]:
        """Tax was withheld at source, so only the net ever reaches the firm and
        it owes the tax to nobody. The net is the customer's money.
        Dr 1100 net / Cr 2010 net.
        """
        cid = p["customer_id"]
        net = money(p["net_amount"])
        return [leg("1100", cid, debit=net), leg("2010", cid, credit=net)]

    def on_dividend_reinvested(self, p: dict, ev: dict) -> list[dict]:
        """The broker reinvests the net directly; cash is never involved. The
        holding grows by a new lot of reinvest_quantity whose cost is the net.
        Dr 1200 net / Cr 2100 net.
        """
        cid, sym = p["customer_id"], p["symbol"]
        net = money(p["net_amount"])
        key = (cid, sym)
        lot = self._new_lot(D(p["reinvest_quantity"]), net)
        self.lots[key].append(lot)
        self.lot_undo[ev["event_id"]] = ("add_lot", key, lot)
        return [leg("1200", cid, debit=net), leg("2100", cid, credit=net)]

    def on_stock_split(self, p: dict, ev: dict) -> list[dict]:
        """No legs. Quantity scales by ratio_to / ratio_from; the total cost of
        each lot is unchanged (cost per share moves).
        """
        cid, sym = p["customer_id"], p["symbol"]
        rf, rt = D(p["ratio_from"]), D(p["ratio_to"])
        lots = self.lots.get((cid, sym))
        if not lots:
            raise Rejected("split of a symbol not held")   # confirm on practice §14
        for l in lots:
            l.qty = qty6(l.qty * rt / rf)                  # cost stays put
        self.lot_undo[ev["event_id"]] = ("split", (cid, sym), rf, rt)
        return []

    def on_symbol_change(self, p: dict, ev: dict) -> list[dict]:
        """No legs. Re-key the holding old_symbol -> new_symbol. A rename into an
        occupied symbol merges the lots, kept in FIFO delivery order by seq.
        """
        cid = p["customer_id"]
        old_key, new_key = (cid, p["old_symbol"]), (cid, p["new_symbol"])
        moving = self.lots.get(old_key)
        if not moving:
            raise Rejected("rename of a symbol not held")  # confirm on practice §14
        existing = self.lots.get(new_key, [])
        self.lot_undo[ev["event_id"]] = ("rename", cid, p["old_symbol"],
                                         p["new_symbol"], list(moving))
        self.lots[new_key] = sorted(existing + moving, key=lambda l: l.seq)
        self.lots.pop(old_key, None)
        return []

    # -- foreign currency (Phase 9) ----------------------------------------
    def on_fx_deposit(self, p: dict, ev: dict) -> list[dict]:
        """Money arrives in another currency and is converted. The omnibus account
        receives the market value; the customer is credited at their (worse) rate;
        the gap is the firm's FX spread, earned now.
        Dr 1100 usd_at_market_rate / Cr 2010 usd_at_customer_rate / Cr 4100 spread.
        A customer rate better than market is a negative spread -- bad data, refused.
        """
        cid = p["customer_id"]
        if D(p["customer_rate"]) > D(p["market_rate"]):
            raise Rejected("fx negative spread: customer rate better than market")
        market = money(p["usd_at_market_rate"])
        customer = money(p["usd_at_customer_rate"])
        spread = market - customer
        if spread < ZERO:
            raise Rejected("fx negative spread")
        return _nonzero([leg("1100", cid, debit=market),
                         leg("2010", cid, credit=customer),
                         leg("4100", cid, credit=spread)])

    # -- corrections: reversals (Phase 8) ----------------------------------
    def on_reversal(self, p: dict, ev: dict) -> list[dict]:
        """Post the exact inverse of the original event's legs (swap debit and
        credit) and keep both, then undo the original's effect on the lot book.
        Reversal of an event never received (or one that posted nothing) is
        refused; the hold is NOT restored -- a released hold stays released.
        """
        orig = p["reverses_event_id"]
        if orig not in self.legs_by_id:
            raise Rejected("reversal of an event never posted")
        if orig in self.reversed:
            raise Rejected("event already reversed")       # confirm on practice §14
        self.reversed.add(orig)
        self._reverse_lots(orig)
        return [leg(l["account"], l["customer_id"],
                    debit=l["credit"], credit=l["debit"])
                for l in self.legs_by_id[orig]]

    def _reverse_lots(self, orig: str) -> None:
        """Undo the lot-book effect the original event had (§9). No-op for events
        that never touched the lot book (cash, dividend_cash, fx, placements).
        """
        undo = self.lot_undo.get(orig)
        if undo is None:
            return
        kind = undo[0]
        if kind == "add_lot":                  # buy fill / reinvest: drop the lot
            _, key, lot = undo
            lots = self.lots.get(key)
            if lots and lot in lots:
                lots.remove(lot)
        elif kind == "sell":                   # restore the exact lots consumed
            _, key, slices = undo
            self._restore_consumed(key, slices)
        elif kind == "split":                  # scale quantities back
            _, key, rf, rt = undo
            for l in self.lots.get(key, []):
                l.qty = qty6(l.qty * rf / rt)
        elif kind == "rename":                 # move the renamed lots back
            _, cid, old, new, moved = undo
            newlots = self.lots.get((cid, new), [])
            moved_set = set(id(l) for l in moved)
            stay = [l for l in newlots if id(l) not in moved_set]
            back = [l for l in newlots if id(l) in moved_set]
            if stay:
                self.lots[(cid, new)] = stay
            else:
                self.lots.pop((cid, new), None)
            self.lots[(cid, old)] = sorted(back + self.lots.get((cid, old), []),
                                           key=lambda l: l.seq)

    def _restore_consumed(self, key, slices) -> None:
        """Put back the FIFO cost a sell relieved, at the lots' original delivery
        positions (by seq). A fully emptied lot is recreated; a partially
        consumed one has its slice added back.
        """
        lots = self.lots.setdefault(key, [])
        by_seq = {l.seq: l for l in lots}
        for seq, take, cost_take, emptied in slices:
            if emptied or seq not in by_seq:
                lot = Lot(take, cost_take, seq)
                lots.append(lot)
                by_seq[seq] = lot
            else:
                lot = by_seq[seq]
                lot.qty += take
                lot.cost += cost_take
        lots.sort(key=lambda l: l.seq)

    # -- resilience & the systematic-defect hunt (Phase 10) ----------------
    def _audit(self, etype: str, p: dict) -> None:
        """Record payload-consistency violations for the defect hunt (§7.8, §10).

        Diagnostic only -- it never rejects; the enumerated bad-data cases have
        their own rejects in the handlers. The feed contains >=1 class of
        internally well-formed but wrong events; run practice through
        tools/replay.py and read these tallies to spot the shared signature,
        then add a targeted reject. Nothing here fires without practice data.
        """
        try:
            if etype in ("order_filled", "order_partially_filled"):
                if money(D(p["quantity"]) * D(p["price"])) != money(p["principal"]):
                    self.stats["audit:principal!=qty*price"] += 1
                br = p.get("broker")
                if br in BROKERS and p.get("asset_class") not in BROKERS[br]["classes"]:
                    self.stats["audit:broker!=asset_class"] += 1
            elif etype in ("dividend_cash", "dividend_reinvested"):
                if money(p["net_amount"]) != money(
                        D(p["gross_amount"]) - D(p["withholding_tax"])):
                    self.stats["audit:net!=gross-withholding"] += 1
            elif etype == "fx_deposit":
                if money(D(p["amount_foreign"]) * D(p["market_rate"])) != money(
                        p["usd_at_market_rate"]):
                    self.stats["audit:usd_market!=amount*rate"] += 1
            elif etype == "interest_credited":
                if money(p["customer_share"]) > money(p["gross_amount"]):
                    self.stats["audit:share>gross"] += 1
        except (KeyError, ValueError, ArithmeticError, TypeError):
            pass          # missing/odd fields are the handler's own concern

    def reconcile(self) -> dict:
        """Final-reconciliation checks a correct book guarantees at stream_end
        (§13): global trial balance zero, no negative position or lot cost, no
        open order carrying a negative hold. Unsettled trades are reported, not
        flagged -- some are legitimately still pending.
        """
        neg_pos = [k for k, lots in self.lots.items()
                   if sum((l.qty for l in lots), D(0)) < 0]
        neg_cost = any(l.cost < 0 for lots in self.lots.values() for l in lots)
        neg_hold = [oid for oid, o in self.orders.items()
                    if not o["closed"] and o["remaining_hold"] < 0]
        return {
            "trial_balance_zero": money(sum(self.balances.values(), ZERO)) == ZERO,
            "negative_positions": neg_pos,
            "negative_lot_cost": neg_cost,
            "negative_holds": neg_hold,
            "unsettled_trades": sum(1 for t in self.trades.values()
                                    if not t["settled"]),
        }

    # -- reporting ----------------------------------------------------------
    def snapshot(self) -> dict:
        """What a checkpoint_request wants: the whole state, debit-positive.

        Report every account ever posted to, including any netted back to zero.
        """
        tb: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for acct in self.posted_accounts:
            tb[acct] = ZERO
        for (_cid, acct), bal in self.balances.items():
            tb[acct] += bal

        customers: dict[str, dict] = {}

        def cust(cid: str) -> dict:
            return customers.setdefault(cid, {"wallet_cash": ZERO,
                                              "cash_hold": ZERO, "positions": {}})

        for (cid, acct), bal in self.balances.items():
            if acct == "2010":
                cust(cid)["wallet_cash"] += -bal    # a liability, credit-positive

        # positions from the lot book: quantity as a plain string, cost basis as
        # the sum of lot total costs. Omit anything that has netted to zero qty.
        for (cid, sym), lots in self.lots.items():
            q = sum((l.qty for l in lots), D(0))
            if q == 0:
                continue
            cost = sum((l.cost for l in lots), ZERO)
            cust(cid)["positions"][sym] = {"quantity": qty_str(q),
                                           "cost_basis": str(money(cost))}

        # cash_hold = remaining buy holds of a customer's still-open orders
        # (sell holds are shares, not cash).
        for o in self.orders.values():
            if not o["closed"] and o["side"] == "buy" and o["remaining_hold"] > 0:
                cust(o["customer_id"])["cash_hold"] += o["remaining_hold"]

        open_routes = {oid: o["route"] for oid, o in self.orders.items()
                       if not o["closed"] and o["route"]}

        return {
            "trial_balance": {a: str(money(v)) for a, v in sorted(tb.items())},
            "customers": {cid: {"wallet_cash": str(money(c["wallet_cash"])),
                                "cash_hold": str(money(c["cash_hold"])),
                                "positions": c["positions"]}
                          for cid, c in sorted(customers.items())},
            "open_order_routes": dict(sorted(open_routes.items())),
        }


# ---------------------------------------------------------------------------
# Ingress: turn every JSON number into Decimal before a handler can see it.
# ---------------------------------------------------------------------------
def _decimalize(obj):
    if isinstance(obj, float):
        # str() first: the shortest round-tripping form, so a feed value like
        # 0.1 becomes Decimal("0.1"), not the exact binary expansion.
        return D(str(obj))
    if isinstance(obj, dict):
        return {k: _decimalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimalize(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Book: the thin wrapper the client calls (idempotency, log, snapshots).
# ---------------------------------------------------------------------------
class Book:
    def __init__(self, dump: str | None = "__env__") -> None:
        self.current = State(strict=False)   # live: a fault rejects, never stalls
        self.log: list[dict] = []      # first-delivery events, in delivery order
        self.seen: set[str] = set()

        # Recorder: with LEDGER_DUMP set, mirror every raw event to a file so
        # unit tests and tools/replay.py run offline, off the network, without
        # spending a practice run. No edit to client.py.
        if dump == "__env__":
            dump = os.environ.get("LEDGER_DUMP")
        self._dump_path: str | None = None
        if dump:
            self._dump_path = "events.jsonl" if dump == "1" else dump
            open(self._dump_path, "w").close()     # truncate a stale capture

    @property
    def todo(self) -> dict[str, int]:
        # client.py prints this at end-of-run to show what is not built yet.
        return self.current.todo

    @property
    def stats(self) -> Counter:
        return self.current.stats

    def apply(self, ev: dict) -> list[dict]:
        # Record the raw event first -- before the seen-check -- so a captured
        # stream faithfully contains the duplicates and replays the server sends.
        if self._dump_path is not None:
            with open(self._dump_path, "a") as f:
                f.write(json.dumps(ev) + "\n")

        eid = ev["event_id"]
        if eid in self.seen:
            return []                  # already delivered; first delivery wins
        self.seen.add(eid)

        ev = _decimalize(ev)           # no float reaches a handler
        self.log.append(ev)
        return self.current.apply(ev)

    def snapshot(self, as_of: str | None = None) -> dict:
        """Current state, or -- for an as-of checkpoint -- the book as it stood
        once ``as_of`` was processed in delivery order, and nothing after it.

        As-of is a pure prefix replay through a fresh ``State``: the same
        ``apply`` builds it, so it cannot diverge from the live book. At <=6,000
        events a from-scratch replay is milliseconds, so no snapshot machinery
        is needed yet.
        """
        if as_of is None:
            return self.current.snapshot()
        s = State(strict=False)          # a live checkpoint must not stall either
        for ev in self.log:
            s.apply(ev)
            if ev.get("event_id") == as_of:
                break
        return s.snapshot()
