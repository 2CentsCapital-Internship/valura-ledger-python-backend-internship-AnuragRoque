#!/usr/bin/env python3
"""Diagnostic practice runner. Runs the real, unmodified client.py but taps the
POST responses so we can read practice's per-event and per-checkpoint feedback
("whether you were right, whether you balanced, which accounts you disagree on"),
which the shipped client discards.

It patches only ``httpx.Client.post`` -- the stream is consumed via
``httpx.Client.stream`` and is never touched, so transport behaviour is
identical to a normal run. Writes ``practice_diag.jsonl``. Pair with LEDGER_DUMP:

    LEDGER_DUMP=1 python tools/practice.py --key ak_... --mode practice

Then analyse practice_diag.jsonl offline. This file is a tuning instrument; the
graded artifact is client.py, which is left exactly as shipped.
"""
from __future__ import annotations

import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_diag = open("practice_diag.jsonl", "w")
_orig_post = httpx.Client.post


def _post(self, url, **kw):
    r = _orig_post(self, url, **kw)
    try:
        if "/v1/postings" in url or "/v1/checkpoint" in url:
            try:
                body = r.json()
            except Exception:
                body = r.text[:4000]
            rec = {"status": r.status_code, "body": body}
            sent = kw.get("json") or {}
            if "postings" in sent:
                rec["kind"] = "postings"
                rec["sent_event_ids"] = [x["event_id"] for x in sent["postings"]]
            elif "checkpoint_id" in sent:
                rec["kind"] = "checkpoint"
                rec["checkpoint_id"] = sent["checkpoint_id"]
            _diag.write(json.dumps(rec, default=str) + "\n")
            _diag.flush()
    except Exception:
        pass
    return r


httpx.Client.post = _post

from client import main  # noqa: E402

sys.exit(main())
