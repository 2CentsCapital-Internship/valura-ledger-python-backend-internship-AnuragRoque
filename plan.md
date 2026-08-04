# plan.md — Ledger Arena: the build plan

The full, phased plan to build the ledger to a top score and defend every line.
Read with [rules.md](rules.md) (the graded facts). Source of truth is the live
protocol (`notes2.txt`). All work is in `book.py`; `client.py` is finished.

> How to use this file: each phase is a self-contained unit of work with a
> **Goal**, **Build**, **Edge cases**, and **Exit criteria** (how *practice*
> proves it done). Do not advance until the exit criteria pass. The Risk register
> lists everything we verify against practice instead of guessing.

## Contents

1. North star & scoring math
2. Architecture (event-sourced, one pure `apply`)
3. Core data structures
4. Numeric & serialization rules (the silent score-killers)
5. The tariff engine — with worked numbers
6. Buy & sell legs — with balance proofs
7. Invariants (correctness net + defect detector)
8. Order lifecycle state machine & holds
9. Reversal semantics (per original type)
10. The systematic-defect hunt (methodology)
11. Phases 0–12
12. Testing & verification
13. Observability & final reconciliation
14. Risk & ambiguity register
15. Self-scoring rubric (the "ATS" view) & traceability matrix
16. Commit map · Module layout · Glossary · Definition of done

---

## 1. North star & scoring math

Correctness is 75/100. **State (checkpoints) 40 > postings 30.** Within a
checkpoint: **cost basis 64%**, firm accounts **11% all-or-nothing**, wallet 9%,
routing 8%, hold 5%, quantity 2%, trial balance 1%. So the expected-value order of
work is unambiguous:

1. **FIFO lot book / cost basis** — the single largest lever (Phase 4).
2. **Fee chain exact on every fill** — 11% flips to 0 on one misclassification
   (Phase 2).
3. **State carried across replay / reversal / split** — what a book of record is
   for (Phases 7–8, 10).
4. **As-of + routing + holds in checkpoints** (Phase 6).

We never guess where practice can tell us. Practice names the accounts we
disagree on; it is the executable spec.

## 2. Architecture: event-sourced, one pure `apply`

The spec forces a design decision up front: **as-of checkpoints and reversals both
need replayable history.** Answer it directly.

**Model.** The book is a fold over an ordered log of first-delivery events.
- `current` = fold `apply` over the whole log.
- `as-of(event_id)` = fold `apply` over the prefix up to and including that event.

As-of is defined as "the book as it stood once you had processed that event, **in
delivery order**, and nothing after it." Delivery (arrival) order is the only
order that matters; `backdated_days` never reorders the log. So as-of is a pure
prefix replay — clean and defensible.

**Two objects.**
- `State` — the whole ledger (balances, lots, registries). One entry point
  `apply(ev) -> list[legs]`: compute legs → validate → mutate → return. **Pure
  function of prior state + event** (no clock, I/O, or randomness), so replay is
  exact.
- `Book` — thin wrapper the client calls. Holds `current: State`, `log`, `seen`.
  `Book.apply` = idempotency + append to log + delegate. `Book.snapshot(as_of)` =
  current snapshot, or fresh-`State` prefix replay for as-of.

**Why maintain current incrementally AND replay for as-of:** a fill's FIFO legs
need the live lot book, so current state is maintained as we ingest (O(1)/event).
As-of is rare and ≤6,000 events, so from-scratch replay is milliseconds — no
snapshot machinery needed. Both paths call the *same* `apply`, so they cannot
diverge; "incremental current == full replay" becomes a test we run.

**Rejection contract — validate-first, mutate-last.** A rejected event produces no
legs and leaves the book *exactly* as it was. Every handler computes legs + the
intended delta, runs all checks, and only then mutates; on failure it raises
`Rejected` before touching state. `apply` wraps handlers so a *parse* failure
(missing/blank field, bad number) also becomes `Rejected`, never a crash — the
most expensive mistake is stopping.

**Idempotency & ordering, centralised:**
- Duplicate/replay: `event_id in seen` → `[]`, no re-apply, no re-log. First
  delivery wins; a conflicting duplicate is ignored, not rejected.
- A redelivered event we already rejected stays one rejection (seen-set is
  outcome-agnostic).
- Out-of-order and backdated events are just their position in the log; handlers
  assume no order beyond FIFO delivery order.

## 3. Core data structures (inside `State`)

```
balances:   dict[(customer_id, account) -> Decimal]        # debit-positive
lots:       dict[(customer_id, symbol) -> list[Lot]]        # FIFO, delivery order
fees:       dict[fee_event_id -> {customer_id, amount}]     # fee_refund lookup
refunded:   set[fee_event_id]                               # double-refund guard
withdrawals:dict[withdrawal_id -> {customer_id, amount, status}]
orders:     dict[order_id -> Order]                         # lifecycle, hold, route
trades:     dict[trade_id -> {side, principal, customer_id, settled}]
legs_by_id: dict[event_id -> list[legs]]                    # reversal: invert accounts
lot_undo:   dict[event_id -> UndoRecord]                    # reversal: invert lot book
reversed:   set[event_id]
posted_accounts: set[account]                              # every account touched
stats:      Counter                                        # per-type, per-reject reason
```

`Order = {customer_id, side, symbol, asset_class, qty, limit_price, est_charges,
filled_qty, closed, route}`. `Lot = {qty: Decimal, cost: Decimal}` — **total
cost**, not per-share (the graded FIFO formula is total-cost based). Balances are
keyed by `(customer_id, account)` — never collapse to account-level;
`transfer_between_customers` nets to zero per account and only the per-customer key
catches it.

## 4. Numeric & serialization rules (the silent score-killers)

These lose marks invisibly if wrong. Lock them in Phase 0.

- **Never float.** JSON numbers must not enter as `float`. Parse the stream with
  `json.loads(..., parse_float=Decimal)` so every number is `Decimal` at ingress.
  `client.py` currently parses without it — this is a legitimate, minimal transport
  fix (or, if we keep client.py untouched, convert every numeric field at the
  handler boundary via `Decimal(str(x))`). Decide in Phase 0; prefer
  `parse_float=Decimal`. Residual float risk at 2 dp is nil either way, but rates
  (up to 6 dp) make the parse-time choice worth getting right.
- **Money** = quantize `0.01`, `ROUND_HALF_UP` (`money()`), half **away from
  zero**, not banker's. **Every derived amount rounded independently.**
- **`bps(principal, n)` = `money(principal * n / 10000)`** rounded to the cent.
- **Quantities** up to 6 dp, emitted as **plain decimal strings** — `"8"`, not
  `"8.000000"` and never `"1E+1"`. Helper `qty_str(q) = format(q.normalize(),
  'f')` (normalize trims trailing zeros; `'f'` forbids scientific notation).
  Verify against the checkpoint example which shows `"quantity": "8"`.
- **Cost basis / cash** = money strings, 2 dp (`"960.00"`).
- **Trial balance** debit-positive; assets positive, liabilities/income negative.

## 5. The tariff engine — with worked numbers

`charges(broker, principal, partner_rate) -> {b,c,r,bc,cc,ps}`, each rounded
independently, computed from the **already-rounded** components:

| sym | meaning | formula |
| --- | --- | --- |
| `b` | brokerage revenue → Cr 4000 | `max(bps(P, brokerage_bps), min_fee)` |
| `c` | custody revenue → Cr 4010 | `bps(P, custody_bps)` |
| `r` | regulatory (owed onward) → Cr 2400 | `bps(P, 8)` |
| `bc`| broker cost → Dr 5000 / Cr 241x | `bps(P, broker_cost_bps) + ticket` |
| `cc`| custody cost → Dr 5010 / Cr 2420 | `bps(P, custody_cost_bps)` |
| `ps`| partner share → Dr 5100 / Cr 2430 | `money(partner_rate * max(0, (b+c) − (bc+cc)))` |

`ps` clamps at 0 — **no clawback**. Partner share is computed from the rounded
`b,c,bc,cc`, then rounded once more.

**Worked example A — profitable buy.** BRK-A (equity), P = 5000.00,
partner_rate = 0.30:
`b = max(10.00, 1.00) = 10.00`, `c = 2.00`, `r = 4.00`,
`bc = 4.50 + 0.35 = 4.85`, `cc = 1.00`,
margin `= (10.00+2.00) − (4.85+1.00) = 6.15`, `ps = 0.30 × 6.15 = 1.845 → 1.85`.

**Worked example B — loss-making small fill (why ~¼ lose money).** BRK-B, P =
200.00, partner_rate = 0.50: `b = max(0.30, 2.50) = 2.50` (min-fee floor),
`c = 0.10`, `r = 0.16`, `bc = 0.16 + 3.00 = 3.16`, `cc = 0.06`,
margin `= 2.60 − 3.22 = −0.62 → max(0,·) = 0`, `ps = 0.00`. The flat ticket
(3.00) dominates → margin negative → partner share zero, no clawback.

**Routing.** `route(asset_class, notional) -> broker`: among brokers trading that
class, minimise customer charge `b + c` on `notional = quantity × limit_price`;
tie → broker id ascending (always exactly one answer). Routing uses the *order's*
limit-price notional; fills carry their own `broker`. Routing only ever matters
for **open** orders (reported at checkpoints).

## 6. Buy & sell legs — with balance proofs

**Buy fill (given, authoritative).** Customer pays P + all customer charges; firm
accrues revenue/cost/reg/partner gross; cash does not move (settles in 2 days).

```
Dr 2010  P + b + c + r        Cr 2350  P
Dr 1200  P                    Cr 2100  P
Dr 5000  bc                   Cr 4000  b
Dr 5010  cc                   Cr 4010  c
Dr 5100  ps                   Cr 2400  r
                              Cr 241x  bc      (241x = payable for this broker)
                              Cr 2420  cc
                              Cr 2430  ps
```
Σ Dr = Σ Cr = `2P + b + c + r + bc + cc + ps`. Add a FIFO lot `{qty, cost = P}`
(commission is the firm's, **never** in cost basis). Record
`trades[trade_id] = {side:"buy", principal:P}`.

**Sell fill (DERIVED — verify on practice).** Proceeds are a **receivable** from
the broker until settlement → **1150**, not 2350 (confirmed by `trade_settled`
sell = `Dr 1100 / Cr 1150`). Custody/claim shrink by FIFO **cost** `k`, not sale
value; the firm's six fee legs are identical in orientation to a buy.

```
Dr 1150  P                    Cr 2010  P − b − c − r
Dr 2100  k                    Cr 1200  k
Dr 5000  bc                   Cr 4000  b
Dr 5010  cc                   Cr 4010  c
Dr 5100  ps                   Cr 2400  r
                              Cr 241x  bc
                              Cr 2420  cc
                              Cr 2430  ps
```
Σ Dr = Σ Cr = `P + k + bc + cc + ps` for any `k`. **Realised P/L =
`(P − b − c − r) − k`**, the residual — never posted.

**Worked example C — sell.** Sell 100 sh, P = 6000.00, FIFO cost k = 5000.00,
BRK-A, partner_rate 0.30: `b=12.00, c=2.40, r=4.80, bc=5.75, cc=1.20, ps=2.24`.
Cr 2010 = `6000 − 12.00 − 2.40 − 4.80 = 5980.80`; both sides sum to 11009.19;
realised P/L = `5980.80 − 5000 = 980.80` (not posted).

## 7. Invariants (correctness net + defect detector)

Hold on every correct book; used as test assertions **and** to find the planted
defect (§10):

1. Every posting balances (Σ Dr == Σ Cr) — enforced in `_post`.
2. No position goes negative; a sell never consumes more than lots hold.
3. FX spread ≥ 0 (`usd_at_market_rate ≥ usd_at_customer_rate`).
4. No settlement pays a zero/negative accumulated balance.
5. A lot's total cost never goes negative after partial relief.
6. Global trial balance sums to zero across all accounts.
7. `fee_refund` amount == referenced `fee_charged`, refunded at most once.
8. **Payload internal consistency** (for defect-hunting): `principal ==
   round(quantity × price)` on fills; `net == gross − withholding` on dividends;
   `usd_at_market_rate == round(amount_foreign × market_rate)` on fx;
   `customer_share ≤ gross` on interest; a fill's `broker` trades its
   `asset_class`.

## 8. Order lifecycle state machine & holds

```
placed ──partially_filled*──▶ (open, filled_qty↑, hold released proportionally)
   │                              │
   │                              └──filled──▶ closed (release remaining → hold 0)
   ├──cancelled──▶ closed (release remaining)
   └──rejected───▶ closed (release remaining)
```
- Placement: **no legs**; buy hold = `qty × limit_price + est_charges`; sell hold =
  `qty` shares. Compute & store `route` now.
- Each fill releases hold **proportional to filled quantity**; `order_filled` is
  the last fill and releases *whatever remains* → hold exactly 0. A closed order
  always returns its hold to 0.
- `cash_hold` at a checkpoint = Σ remaining **buy** holds of that customer's open
  orders (sell holds are shares, not cash).
- **Reversing a fill does NOT restore the hold** — reversal undoes postings + lots,
  not lifecycle.
- Out-of-order: a fill before its placement lazily creates/patches the `Order`;
  when placement arrives, reconcile `placed_qty` and hold. (Confirm hold behavior
  for the never-placed case on practice.)

## 9. Reversal semantics (per original type)

`reversal(reverses_event_id)`: post the exact inverse of `legs_by_id[orig]`
(swap debit/credit), keep both, then apply `lot_undo[orig]`:

| Original | Account inverse | Lot-book undo |
| --- | --- | --- |
| deposit / fee / withdrawal* / interest / transfer / dividend_cash / fx | swap legs | none |
| buy fill | swap legs | remove the lot that fill added |
| sell fill | swap legs | **restore** the exact lots it consumed, at original FIFO positions (from `lot_undo`) |
| dividend_reinvested | swap legs | remove the reinvest lot |
| stock_split | none (no legs) | multiply back by `ratio_from/ratio_to` |
| symbol_change | none | rename back `new→old` |

Every handler that mutates lots records an `UndoRecord` keyed by `event_id`, so
reversal is O(1) and local. Reversal of an event **never received** → `Rejected`.
Reversal of a no-leg event (placement) → no legs, valid. Hold is **not** restored.
(Double reversal, and reversal of an already-refunded/reversed event: confirm on
practice.)

## 10. The systematic-defect hunt (methodology)

The feed contains ≥1 class of events that are **internally well-formed but wrong**,
findable only via invariants — distinct from the enumerated rejections (oversell,
unknown ref, negative spread, zero-settle, malformed). Method:

1. Run practice with an **invariant + payload-consistency audit** (§7.8) that, for
   every event, records any relation it violates *without* yet rejecting.
2. Inspect offenders offline in `events.jsonl`; find the shared signature (e.g. a
   whole class where `principal ≠ quantity × price`, or a broker executing an
   asset class it doesn't cover).
3. Reject exactly that class; keep the audit on in submission as a safety net.
4. Guard against **false positives** — a legitimate rounding gap is not a defect;
   the defect is *systematic*, so require the relation to fail by more than a cent
   or across a consistent field.

## 11. Phases

Ordered so each is runnable against practice and the highest-value pieces land
first. Format: **Goal → Build → Edge cases → Exit criteria.**

### Phase 0 — Foundation & harness
**Goal.** Event-sourced skeleton with no behavior change; offline test loop.
**Build.** Split `book.py` into `Book`(log/seen/snapshot) + `State`(apply); keep
`on_deposit` identical. Add `money`/`bps`/`leg`/`qty_str`, account + broker tables,
empty registries. Decide float ingress (§4). Validate-first plumbing; malformed →
`Rejected`. **Recorder:** when `LEDGER_DUMP` env set, `Book.apply` appends each raw
event to `events.jsonl` (no edit to client.py). **Runner:** `tools/replay.py` folds
`events.jsonl` through a fresh `Book`, prints todo + global-TB-zero check.
**Exit.** Practice unchanged (deposits post, rest skipped); `events.jsonl`
captured; replay reproduces counts; global TB = 0.

### Phase 1 — Cash events
**Goal.** All non-trade cash correct.
**Build.** `fee_charged` (`Dr 2010/Cr 1100`, record fee by id), `fee_refund`
(lookup by `refunds_source_id`, `Dr 1100/Cr 2010`, mark refunded),
`withdrawal_requested/settled/rejected` (registry by `withdrawal_id`),
`interest_credited` (`Dr 1100 gross / Cr 2010 share / Cr 4200 gross−share`),
`transfer_between_customers` (`Dr 2010 from / Cr 2010 to`).
**Edge.** Double refund → `Rejected`. Unknown ref → `Rejected`. Interest with
gross==share → 4200 leg = 0 (omit vs post: confirm). Transfer to self → `Rejected`
(likely defect) — confirm.
**Exit.** Practice: every cash event correct & balanced; transfer moves two
wallets while account 2010 stays net-zero.

### Phase 2 — Tariff engine (firm accounts: all-or-nothing)
**Goal.** Pure, tested `charges()` + `route()`.
**Build.** §5. Unit tests: min-fee floor, ticket in cost, negative margin → ps=0,
half-cent partner rounding, routing tie-break.
**Exit.** A practice buy fill scores the firm-accounts block
(2400/241x/2420/2430/4000/4010/5000/5010/5100) correct as one block.

### Phase 3 — Order lifecycle & holds
**Goal.** Placements/cancels (no legs) tracked; buy fills posted; holds correct.
**Build.** §8. Split `order_partially_filled` vs `order_filled` (the shipped stub
wrongly aliases them). Buy fill legs (§6) + add lot + record trade. Store `route`
at placement.
**Edge.** Fill before placement (lazy order); over-fill (invariant); backdated fill
posts normally.
**Exit.** Practice buy fills correct; placed→partial→filled order returns
`cash_hold` to 0 after close; open orders report right `route`.

### Phase 4 — FIFO lot book & sells (**biggest lever**)
**Goal.** Cost relief to the cent in delivery order; oversell rejected cleanly.
**Build.** Sell legs (§6). `fifo_consume(cust, sym, qty)`: **validate full quantity
available before mutating**; per partial lot `relief += round(lot.cost × take /
lot.qty)`, decrement, drop emptied lots; record consumption in `lot_undo` for
reversal. Store trade.
**Edge.** Oversell / no-lot sale → `Rejected`, zero mutation. Exact vs partial lot
both use the total-cost formula (never cost-per-share).
**Exit.** Practice sell legs correct; position cost_basis & quantity match
reference; an oversell is a correct rejection leaving the position intact.

### Phase 5 — trade_settled
**Goal.** Discharge settlement obligation.
**Build.** Lookup by `trade_id`: buy → `Dr 2350/Cr 1100 P`; sell → `Dr 1100/Cr
1150 P`; mark settled.
**Edge.** Unknown trade_id → reject vs hold-pending (confirm). Duplicate →
idempotent.
**Exit.** 2350/1150 return to zero for that trade; 1100 moves by P; practice
correct.

### Phase 6 — Checkpoints (40% of score)
**Goal.** Full snapshot incl. routes & as-of.
**Build.** `trial_balance` (every posted account incl. zeroed, debit-positive);
`customers[cid]` = `wallet_cash = −balance(cid,2010)`, `cash_hold = Σ open buy
holds`, `positions` (per symbol: quantity via `qty_str`, cost_basis via `money`,
**omit zero-qty**); `open_order_routes` for open orders; `Book.snapshot(as_of)`
replays a fresh `State` over the prefix.
**Edge.** Quantity formatting (§4). Customer with only closed positions →
wallet_cash but no positions. Report accounts netted to zero.
**Exit.** Practice checkpoint breakdown near-full on every sub-component; an as-of
checkpoint matches historical, not current, state.

### Phase 7 — Corporate actions (small-looking, high-impact)
**Goal.** Per-customer dividends/reinvest/split/rename.
**Build.** `dividend_cash` (`Dr 1100/Cr 2010 net`; no tax payable);
`dividend_reinvested` (`Dr 1200/Cr 2100 net` + add lot @ cost=net; no cash);
`stock_split` (no legs; scale lots `qty *= to/from`, total cost unchanged);
`symbol_change` (no legs; re-key). All **per named customer only**; record
`lot_undo`.
**Edge.** Rename into occupied symbol → merge lots preserving FIFO order. Fractional
split shares → Decimal 6 dp. Action on unheld symbol → confirm.
**Exit.** Positions/cost basis stay correct across a split and a rename.

### Phase 8 — Reversals
**Goal.** Inverse legs + lot-book undo (§9).
**Build.** §9 table. Unknown-ref → `Rejected`.
**Exit.** After a reversed buy, position and all later cost basis match a run where
the buy never happened; unknown-ref reversal is a correct rejection.

### Phase 9 — fx_deposit
**Goal.** Conversion + spread + negative-spread rejection.
**Build.** `Dr 1100 usd_at_market / Cr 2010 usd_at_customer / Cr 4100 diff`.
**Reject if customer_rate > market_rate.**
**Exit.** Practice fx correct; negative-spread fx a correct rejection; 4100 accrues.

### Phase 10 — Resilience & the defect
**Goal.** Survive everything; find & reject the defect class.
**Build.** Verify idempotency across a forced reset (checkpoints byte-identical
before/after). Field-presence guards → reject not crash. Defect hunt per §10.
**Exit.** Resilience full; defect class rejected with no false positives; never
stalls.

### Phase 11 — Verify, tune, submit
**Goal.** Drive the score up, lock a submission.
**Build.** Offline unit tests per handler + the "incremental == replay" invariant.
Practice loop (12 runs): read diffs/breakdown, fix named disagreements, repeat.
Then one `submission`, tune, keep best; then `final`. Write `MISSING.md` iff
anything is cut.
**Exit.** Stable high practice; submission matches within noise; no crashes/stalls;
checkpoints on time.

### Phase 12 — Latency & batching (last)
**Goal.** Confirm liveness only. Client already batches ≤500 and snapshots first.
Verify p95 < 5 s and as-of replay ≪ grace at 6,000 events. Optimise only if
measured (e.g. cache as-of snapshots) — not before.

## 12. Testing & verification
1. **Record once, test forever** — `LEDGER_DUMP=1` → `events.jsonl`; unit tests &
   `replay.py` run offline, no network, no spent practice runs.
2. **Golden invariants** (§7) asserted on every replay.
3. **Determinism test** — current-after-ingest == full replay == as-of(last). A
   divergence means an in-place mutation leaked; fix before trusting checkpoints.
4. **Practice as oracle** — every exit criterion is "practice says the named
   accounts agree." Believe practice over our reading of the prose.

## 13. Observability & final reconciliation
- **Stats** (in `State`, printed like the todo list): counts per event type, per
  rejection reason, invariant-violation log — no edit to client.py.
- **Final reconciliation (5%)** at `stream_end`: assert global TB = 0; every trade
  either settled or pending (none orphaned); no negative positions; no open order
  with a negative hold. Emit a one-screen reconciliation report.

## 14. Risk & ambiguity register (confirm on practice — don't guess)
- **Ticket fee** placement: cost (`bc`) — our reading, explains loss-making fills —
  vs brokerage revenue. Verify with a practice buy fill; flip if firm block
  disagrees.
- **Sell legs** are derived (§6): confirm 1150/2010/2100/1200 orientation on
  practice before trusting the 11% block.
- **Zero-value legs** (interest with no firm share; ps=0): post `0.00` or omit?
  Default: omit genuinely-zero legs unless practice wants them.
- **Unknown `trade_settled`/`withdrawal_settled`** ordering: reject vs hold-pending.
- **Transfer to self, split/rename of unheld symbol, rename into occupied symbol,
  double reversal, refund of a reversed fee** — confirm each.
- **Routing notional** uses order `quantity × limit_price`; confirm routing only
  matters for open orders.
- **est_charges** used as given for the hold; not recomputed.

## 15. Self-scoring rubric (the "ATS" view) & traceability matrix

Read this as the grader would: every scoring line maps to a deliverable and the
evidence that proves it. If every row is green on practice, the score is maxed.

| Scoring component | Wt | Deliverable(s) | Phase | Proof / how graded green |
| --- | --- | --- | --- | --- |
| Posting correctness | 30 | every handler | 1–9 | practice per-event "correct + balanced" for all types |
| Checkpoint: cost basis | 40×64% | FIFO lot book, split/rename/reversal undo | 4,7,8 | cost_basis matches ref across split/rename/reversal |
| Checkpoint: firm accounts | 40×11% | tariff engine (all-or-nothing) | 2 | firm block correct on every fill |
| Checkpoint: wallet cash | 40×9% | 2010 per-customer keying | 1,6 | wallet_cash correct incl. after transfer |
| Checkpoint: routing | 40×8% | `route()`, open-order tracking | 2,3,6 | open_order_routes correct |
| Checkpoint: cash hold | 40×5% | hold state machine | 3,6 | hold → 0 on close; partials proportional |
| Checkpoint: quantity | 40×2% | lot quantity + `qty_str` | 4,6 | quantity strings correct format |
| Checkpoint: trial balance | 40×1% | posted_accounts, debit-positive | 6 | TB matches, zeros included |
| Resilience | 15 | seen-set, replay, rejections, defect | 0,4,8,10 | idempotent across reset; defect rejected, no false positives |
| Liveness | 10 | client batching, snapshot-first | 12 | p95 < 5 s; checkpoints on time |
| Final reconciliation | 5 | recon report | 13 | global TB=0, no orphans at stream_end |

**Traceability:** every event type → handler → test → practice exit criterion.
No event type ships without (a) a unit test on `events.jsonl` and (b) a green
practice diagnostic.

## 16. Commit map · Module layout · Glossary · Definition of done

**Commit map** (all authored by Anurag; terse technical; only when asked):
`p0 event-sourced skeleton + offline recorder` · `p1 cash events` · `p2 tariff
engine and routing` · `p3 order lifecycle and holds` · `p4 fifo lot book and
sells` · `p5 trade settlement` · `p6 checkpoints with routes and as-of` · `p7
corporate actions` · `p8 reversals` · `p9 fx deposit` · `p10 resilience and defect
rejection` · `p11 tests and tuning`.

**Module layout.** Keep a single graded `book.py` (they read the repo; one file is
easiest to walk and defend). Non-graded helpers live in `tools/` (`replay.py`,
tests) and never import from a place that couples them to transport.

**Glossary.** *Principal* = fill price × quantity. *Lot* = a purchase parcel
(qty + total cost). *FIFO cost relief* = cost removed when selling, oldest lot
first, `round(lot.cost × take / lot.qty)`. *Hold* = un-spendable cash / un-sellable
shares reserved by an open order. *Route* = the cheapest eligible broker for an
open order. *As-of* = book state at a past event in delivery order. *Defect* = a
systematically wrong-but-well-formed event class we detect and reject.

**Definition of done.**
- [ ] All 20+ event types handled or explicitly, defensibly rejected.
- [ ] Firm-accounts block correct (all-or-nothing) on practice.
- [ ] Cost basis + quantity correct across split, rename, reversal, replay.
- [ ] As-of checkpoints correct; routes correct; holds → 0 on close.
- [ ] Idempotent across forced reconnect; defect class rejected, no false
      positives; never stalls.
- [ ] Offline tests green; determinism invariant holds; recon clean at stream_end.
- [ ] `MISSING.md` written iff anything cut.
- [ ] History is Anurag's, terse & technical; every line defensible live.
```
