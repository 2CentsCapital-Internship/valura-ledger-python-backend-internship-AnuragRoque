#!/usr/bin/env python3
"""Fold a captured event stream through a fresh Book, offline.

Record once with ``LEDGER_DUMP=1 python client.py ... --mode practice`` (writes
``events.jsonl``), then replay it here as many times as you like without
touching the network or spending a practice run:

    python tools/replay.py [events.jsonl]

It reports what is handled vs still todo, the per-outcome tallies, and the two
invariants that must hold on any correct book at Phase 0: every posting
balanced (checked inside ``_post``) and the global trial balance summing to
zero.
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal

# Import book.py whether run from the repo root or from tools/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from book import Book, money  # noqa: E402

ZERO = Decimal("0.00")


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "events.jsonl"
    if not os.path.exists(path):
        print(f"no capture at {path!r}. Record one first:")
        print("  LEDGER_DUMP=1 python client.py --url <url> --key <key> --mode practice")
        return 1

    book = Book(dump=None)             # replaying: do not re-record
    n = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            book.apply(json.loads(line))
            n += 1

    stats = book.stats
    types = sorted({t for t in stats if ":" not in t})
    print(f"replayed {n} events ({len(book.seen)} unique) from {path}\n")
    print(f"{'event type':<30}{'seen':>7}{'posted':>8}{'reject':>8}"
          f"{'malf':>7}{'todo':>7}")
    for t in types:
        print(f"{t:<30}{stats[t]:>7}{stats['posted:' + t]:>8}"
              f"{stats['reject:' + t]:>8}{stats['malformed:' + t]:>7}"
              f"{book.todo.get(t, 0):>7}")

    todo_total = sum(book.todo.values())
    if todo_total:
        print(f"\nnot implemented yet ({todo_total} events skipped): "
              + ", ".join(f"{t}={n}" for t, n in sorted(book.todo.items())))

    # Global trial balance: every posting balances, so the debit-positive sum
    # over all (customer, account) balances must be exactly zero.
    total = sum(book.current.balances.values(), ZERO)
    ok = money(total) == ZERO
    print(f"\nglobal trial balance: {money(total)}  -> {'OK (zero)' if ok else 'NON-ZERO'}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
