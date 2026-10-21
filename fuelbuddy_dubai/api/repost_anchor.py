"""
The repost-anchor rule (IDEV-3156).

Deliberately dependency-free — no frappe, no DB — so the one rule that governs
whether a queued stock-valuation repair survives can be tested without a
bench, a site or a database. `dn_drain` holds the DB shell around it.

Background: parking a pending Repost Item Valuation without adopting its anchor
silently destroys a repair ERPNext had already queued. Production reached 1,534
Skipped reposts (anchors 2026-06-01 to 2026-09-01) against 3 Completed, leaving
22 unhealed valuation-chain breaks.
"""

from __future__ import annotations


class AnchorTooEarly(Exception):
    """Raised when the resolved anchor implies more work than the caller allows.

    Carries the resolved anchor so the caller can report it or re-run with an
    explicit override.
    """

    def __init__(self, anchor, floor):
        self.anchor = anchor
        self.floor = floor
        super().__init__(
            f"resolved repost anchor {anchor} is earlier than the permitted "
            f"floor {floor}; a walk from there may run for days. Re-run with "
            f"an explicit anchor_floor to accept it."
        )


def resolve_repost_anchor(batch_earliest, pending_anchors, anchor_floor=None):
    """Earliest posting_date a consolidated repost must start from.

    A consolidated repost supersedes the reposts it parks, so it has to start
    at or before the earliest of them. Anchor it later and their work is lost
    with no trace: the rows they would have re-walked keep stale values, and
    nothing remains queued to fix them.

    The floor exists because inheritance is unbounded by nature. Pending
    anchors observed in production reach back to 2026-01-27, and a walk from
    there is ~513k rows — days of work, which a drain must not commit to
    silently. Passing `anchor_floor` makes the caller state how far back it is
    willing to go; going further raises rather than proceeding.

    Args:
        batch_earliest:  earliest posting_date in the drained batch, or None
        pending_anchors: iterable of posting_date for every superseded repost;
                         None entries are ignored
        anchor_floor:    earliest date the caller accepts, or None for no limit

    Returns:
        The minimum non-null date, or None when there is nothing to anchor on.

    Raises:
        AnchorTooEarly: the minimum is earlier than `anchor_floor`.
    """
    candidates = [d for d in list(pending_anchors) + [batch_earliest] if d]
    if not candidates:
        return None

    anchor = min(candidates)
    if anchor_floor and anchor < anchor_floor:
        raise AnchorTooEarly(anchor, anchor_floor)
    return anchor
