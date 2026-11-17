"""
Run-key tests for the DN drain shadow Bin (IDEV-3266).

`fb_drain:run:active` serialises the drain against itself and against the
quantity-correction amend in fuelbuddy_crm. These tests need Redis (the
site's redis_cache) but no ERPNext data:

    bench --site <site> run-tests --module fuelbuddy_dubai.tests.test_shadow_bin_run_key
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import frappe
import redis
from frappe.tests.utils import FrappeTestCase

from fuelbuddy_dubai.api import shadow_bin
from fuelbuddy_dubai.api.shadow_bin import RUN_KEY, acquire_run_key, release_run_key


class TestRunKey(FrappeTestCase):
    def setUp(self):
        self.r = shadow_bin._raw_redis()
        if self.r.get(RUN_KEY):
            self.skipTest(f"{RUN_KEY} is held by a real run on this site: {self.r.get(RUN_KEY)}")

    def tearDown(self):
        self.r.delete(RUN_KEY)
        for k in self.r.scan_iter(match=f"{shadow_bin.KEY_PREFIX}qc-test*"):
            self.r.delete(k)

    # ---- acquire / release -------------------------------------------------
    def test_acquire_when_free_sets_holder_and_ttl(self):
        self.assertTrue(acquire_run_key("holder-a", 60))
        self.assertEqual(self.r.get(RUN_KEY), "holder-a")
        self.assertTrue(0 < self.r.ttl(RUN_KEY) <= 60)

    def test_acquire_when_held_fails_and_keeps_holder(self):
        self.assertTrue(acquire_run_key("holder-a", 60))
        self.assertFalse(acquire_run_key("holder-b", 60))
        self.assertEqual(self.r.get(RUN_KEY), "holder-a")

    def test_release_only_by_owner(self):
        acquire_run_key("holder-a", 60)
        self.assertFalse(release_run_key("holder-b"))
        self.assertEqual(self.r.get(RUN_KEY), "holder-a")
        self.assertTrue(release_run_key("holder-a"))
        self.assertIsNone(self.r.get(RUN_KEY))

    def test_release_when_absent_is_false(self):
        self.assertFalse(release_run_key("holder-a"))

    def test_concurrent_acquire_has_exactly_one_winner(self):
        """The race the old get-then-set lost: many contenders released at the
        same instant, repeated; SET NX lets exactly one through every time."""
        # frappe.conf is thread-local: resolve the connection details here and
        # hand the worker threads plain redis-py connections.
        url = frappe.conf.get("redis_cache") or "redis://redis-cache:6379"
        with patch.object(shadow_bin, "_raw_redis", lambda: redis.from_url(url, decode_responses=True)):
            self._race_rounds()

    def _race_rounds(self):
        for round_ in range(30):
            contenders = 8
            barrier = threading.Barrier(contenders)
            wins = []

            def contend(i, barrier=barrier, wins=wins, round_=round_):
                barrier.wait()
                if acquire_run_key(f"holder-{round_}-{i}", 60):
                    wins.append(i)

            threads = [threading.Thread(target=contend, args=(i,)) for i in range(contenders)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(wins), 1, f"round {round_}: winners {wins}")
            self.assertEqual(self.r.get(RUN_KEY), f"holder-{round_}-{wins[0]}")
            self.r.delete(RUN_KEY)

    # ---- initialize ---------------------------------------------------------
    def test_initialize_takes_the_key_with_drain_ttl(self):
        shadow_bin.initialize("qc-test-run-1", [])
        self.assertEqual(self.r.get(RUN_KEY), "qc-test-run-1")
        self.assertTrue(0 < self.r.ttl(RUN_KEY) <= shadow_bin.RUN_KEY_TTL_S)

    def test_initialize_refuses_when_another_holder_has_the_key(self):
        acquire_run_key("qc-amend:episode-1:abcd", 60)
        with self.assertRaises(frappe.ValidationError):
            shadow_bin.initialize("qc-test-run-1", [])
        self.assertEqual(self.r.get(RUN_KEY), "qc-amend:episode-1:abcd")

    def test_initialize_same_run_is_reentrant(self):
        shadow_bin.initialize("qc-test-run-1", [])
        shadow_bin.initialize("qc-test-run-1", [])
        self.assertEqual(self.r.get(RUN_KEY), "qc-test-run-1")

    def test_initialize_cannot_overwrite_even_if_both_saw_it_free(self):
        """Deterministic replay of the old race: drain B's read says the key is
        free (as it would have been a moment earlier), but drain A has taken it
        since. Get-then-set overwrote A; SET NX refuses B."""
        shadow_bin.initialize("qc-test-run-a", [])
        real = shadow_bin._raw_redis

        def stale_reader():
            conn = real()
            conn.get = lambda key: None
            return conn

        with patch.object(shadow_bin, "_raw_redis", stale_reader):
            with self.assertRaises(frappe.ValidationError):
                shadow_bin.initialize("qc-test-run-b", [])
        self.assertEqual(self.r.get(RUN_KEY), "qc-test-run-a")

    # ---- cleanup --------------------------------------------------------------
    def _shadow(self, run_id):
        self.r.hset(f"{shadow_bin.KEY_PREFIX}qc-test-item:wh", mapping={"drain_run_id": run_id})

    def test_cleanup_by_owner_clears_shadow_and_releases(self):
        shadow_bin.initialize("qc-test-run-1", [])
        self._shadow("qc-test-run-1")
        self.assertGreaterEqual(shadow_bin.cleanup("qc-test-run-1"), 1)
        self.assertIsNone(self.r.get(RUN_KEY))
        self.assertFalse(self.r.exists(f"{shadow_bin.KEY_PREFIX}qc-test-item:wh"))

    def test_cleanup_by_non_owner_touches_nothing(self):
        """A drain whose initialize() lost to another holder runs cleanup in its
        exception path; it must not wipe the winner's shadow or key."""
        shadow_bin.initialize("qc-test-winner", [])
        self._shadow("qc-test-winner")
        self.assertEqual(shadow_bin.cleanup("qc-test-loser"), 0)
        self.assertEqual(self.r.get(RUN_KEY), "qc-test-winner")
        self.assertTrue(self.r.exists(f"{shadow_bin.KEY_PREFIX}qc-test-item:wh"))

    def test_late_cleanup_does_not_release_a_newer_holder(self):
        """Our key expired mid-run and an amend took it; our late cleanup must
        leave the amend's key alone."""
        shadow_bin.initialize("qc-test-run-1", [])
        self.r.delete(RUN_KEY)  # TTL expiry
        self.assertTrue(acquire_run_key("qc-amend:episode-2:ffff", 60))
        shadow_bin.cleanup("qc-test-run-1")
        self.assertEqual(self.r.get(RUN_KEY), "qc-amend:episode-2:ffff")

    def test_cleanup_without_run_id_is_a_no_op(self):
        acquire_run_key("qc-amend:episode-3:0000", 60)
        self.assertEqual(shadow_bin.cleanup(None), 0)
        self.assertEqual(self.r.get(RUN_KEY), "qc-amend:episode-3:0000")
