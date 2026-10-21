"""
Regression tests for the DN drain repost anchor (IDEV-3156).

These are deliberately dependency-free: `resolve_repost_anchor` is pure, so the
rule that matters can be tested with plain python, no site, no bench, no
database. Run with:

    python3 -m unittest discover -s fuelbuddy_dubai/tests -v

The integration half — that a drain over a LATER window adopts an EARLIER
pending anchor end to end — needs a live site and lives in
`test_dn_drain_integration.py`, which is run on the local replica.

Why this exists
---------------
Production accumulated 1,534 Skipped Repost Item Valuations with anchors from
2026-06-01 to 2026-09-01 against only 3 Completed, and 22 unhealed
valuation-chain breaks. Cause: the drain parked pending reposts and then
anchored its replacement at the earliest posting_date of its OWN batch, so
every earlier anchor was discarded silently.
"""

from __future__ import annotations

import datetime
import importlib.util
import pathlib
import unittest

# Loaded by path, not by package import: fuelbuddy_dubai/__init__.py imports
# frappe at module load, and the point of this test is to need none of it.
_MOD = pathlib.Path(__file__).resolve().parents[1] / "api" / "repost_anchor.py"
_spec = importlib.util.spec_from_file_location("repost_anchor", _MOD)
_repost_anchor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_repost_anchor)
resolve_repost_anchor = _repost_anchor.resolve_repost_anchor


def d(iso: str) -> datetime.date:
    return datetime.date.fromisoformat(iso)


class TestResolveRepostAnchor(unittest.TestCase):
    """The invariant: never anchor later than anything superseded."""

    def test_adopts_earlier_pending_anchor(self):
        """THE regression. Fails on pre-IDEV-3156 code, which returned the
        batch date and orphaned the June repair."""
        anchor = resolve_repost_anchor(
            batch_earliest=d("2026-08-01"),
            pending_anchors=[d("2026-06-09")],
        )
        self.assertEqual(anchor, d("2026-06-09"))

    def test_keeps_batch_date_when_it_is_earliest(self):
        anchor = resolve_repost_anchor(
            batch_earliest=d("2026-06-01"),
            pending_anchors=[d("2026-08-15"), d("2026-09-01")],
        )
        self.assertEqual(anchor, d("2026-06-01"))

    def test_picks_minimum_across_many(self):
        """Mirrors the real Skipped spread: 2026-06-01 to 2026-09-01."""
        anchor = resolve_repost_anchor(
            batch_earliest=d("2026-08-27"),
            pending_anchors=[
                d("2026-09-01"), d("2026-06-11"), d("2026-07-04"),
                d("2026-06-01"), d("2026-08-08"),
            ],
        )
        self.assertEqual(anchor, d("2026-06-01"))

    def test_no_pending_anchors_uses_batch(self):
        anchor = resolve_repost_anchor(d("2026-08-01"), [])
        self.assertEqual(anchor, d("2026-08-01"))

    def test_ignores_null_anchors(self):
        """An RIV with a null posting_date must not poison the min()."""
        anchor = resolve_repost_anchor(
            batch_earliest=d("2026-08-01"),
            pending_anchors=[None, d("2026-06-09"), None],
        )
        self.assertEqual(anchor, d("2026-06-09"))

    def test_null_batch_still_adopts_pending(self):
        """Batch date can be null if every draft failed pre-flight."""
        anchor = resolve_repost_anchor(None, [d("2026-06-09")])
        self.assertEqual(anchor, d("2026-06-09"))

    def test_nothing_to_anchor_on_returns_none(self):
        self.assertIsNone(resolve_repost_anchor(None, []))
        self.assertIsNone(resolve_repost_anchor(None, [None]))

    def test_is_pure(self):
        """No mutation of the caller's list — it is reused by the caller."""
        pending = [d("2026-06-09")]
        resolve_repost_anchor(d("2026-08-01"), pending)
        self.assertEqual(pending, [d("2026-06-09")])

    def test_the_four_stuck_june_reposts(self):
        """The real production case: the four In-Progress RIVs stuck on
        DN 236061 / 236200 / 240734, against an August drain window.

        Their anchors must be adopted, or the three June chain breaks
        (-18,355.1235 / -17,375.8840 / -435.6586) stay unhealed."""
        stuck = [d("2026-06-09"), d("2026-06-09"), d("2026-06-09"), d("2026-06-11")]
        anchor = resolve_repost_anchor(d("2026-08-27"), stuck)
        self.assertEqual(anchor, d("2026-06-09"))



class TestAnchorFloor(unittest.TestCase):
    """The floor stops inheritance committing to a walk nobody sized.

    Measured against production: an anchor of 2026-06-09 implies 288,625 SLE
    rows (~48h at 0.6s/row); 2026-01-27 implies 513,248 (~85h). A restarted
    Failed repost carries a January anchor, so without a floor one drain call
    can silently become a three-day inline walk.
    """

    def test_within_floor_returns_anchor(self):
        anchor = _repost_anchor.resolve_repost_anchor(
            d("2026-08-01"), [d("2026-06-09")], anchor_floor=d("2026-06-01")
        )
        self.assertEqual(anchor, d("2026-06-09"))

    def test_earlier_than_floor_raises(self):
        with self.assertRaises(_repost_anchor.AnchorTooEarly) as ctx:
            _repost_anchor.resolve_repost_anchor(
                d("2026-08-01"), [d("2026-01-27")], anchor_floor=d("2026-06-01")
            )
        self.assertEqual(ctx.exception.anchor, d("2026-01-27"))
        self.assertEqual(ctx.exception.floor, d("2026-06-01"))

    def test_exactly_on_floor_is_allowed(self):
        anchor = _repost_anchor.resolve_repost_anchor(
            d("2026-08-01"), [d("2026-06-01")], anchor_floor=d("2026-06-01")
        )
        self.assertEqual(anchor, d("2026-06-01"))

    def test_no_floor_means_no_limit(self):
        anchor = _repost_anchor.resolve_repost_anchor(
            d("2026-08-01"), [d("2026-01-27")]
        )
        self.assertEqual(anchor, d("2026-01-27"))

    def test_floor_not_applied_when_nothing_to_anchor_on(self):
        self.assertIsNone(
            _repost_anchor.resolve_repost_anchor(None, [], anchor_floor=d("2026-06-01"))
        )


if __name__ == "__main__":
    unittest.main()
