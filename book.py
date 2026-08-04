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


class Rejected(Exception):
    """Raise from a handler for an event you refuse to post on its own merits.

    An oversell, a reversal of something never received, a payload that will not
    parse. A rejected event produces no legs and must leave the book exactly as
    it was. Rejecting one event and carrying on beats stopping.
    """


class Lot:
    """A FIFO purchase parcel: a total cost against a quantity.

    The graded FIFO formula relieves ``round(cost * sold / qty)`` from the total,
    so we carry the *total* cost, never a cost-per-share.
    """
    __slots__ = ("qty", "cost")

    def __init__(self, qty: Decimal, cost: Decimal) -> None:
        self.qty = D(qty)
        self.cost = D(cost)


# ---------------------------------------------------------------------------
# State: the whole ledger, and the one place events are interpreted.
# ---------------------------------------------------------------------------
class State:
    def __init__(self) -> None:
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
        except (KeyError, ValueError, ArithmeticError, TypeError):
            # A payload that will not parse is bad data: refuse it, carry on.
            self.stats["malformed:" + etype] += 1
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

    # Everything else is unimplemented on purpose: with no handler it routes to
    # ``todo`` and scores zero for that event without stopping the run. Handlers
    # land phase by phase (see plan.md section 11).

    # -- reporting ----------------------------------------------------------
    def snapshot(self) -> dict:
        """What a checkpoint_request wants: the whole state, debit-positive.

        Report every account ever posted to, including any netted back to zero.
        (Positions, holds and routes fill in from Phase 3 on; here only deposits
        have posted, so those stay empty.)
        """
        tb: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for acct in self.posted_accounts:
            tb[acct] = ZERO
        for (_cid, acct), bal in self.balances.items():
            tb[acct] += bal

        customers: dict[str, dict] = {}
        for (cid, acct), bal in self.balances.items():
            c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                           "cash_hold": ZERO, "positions": {}})
            if acct == "2010":
                c["wallet_cash"] += -bal        # a liability, so credit-positive

        return {
            "trial_balance": {a: str(money(v)) for a, v in sorted(tb.items())},
            "customers": {cid: {"wallet_cash": str(money(c["wallet_cash"])),
                                "cash_hold": str(money(c["cash_hold"])),
                                "positions": c["positions"]}
                          for cid, c in sorted(customers.items())},
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
        self.current = State()
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
        s = State()
        for ev in self.log:
            s.apply(ev)
            if ev.get("event_id") == as_of:
                break
        return s.snapshot()
