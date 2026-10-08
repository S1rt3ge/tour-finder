"""Deal evidence must describe a recent drop of a comparable, available offer."""
from datetime import datetime, timezone
import unittest

from tourfinder.deals import assess


NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def point(when, price=100_000, currency="EUR"):
    return {"fetched_at": when, "price_cents": price, "currency": currency}


class DealTests(unittest.TestCase):
    def setUp(self):
        self.offer = dict(source="joinup", price_cents=80_000, currency="EUR",
                          fetched_at="2026-10-08T10:00:00Z", date_start="2026-10-20",
                          stop_sale=None, review_rating=4.5, review_scale=5,
                          review_count=100, review_match_status="ok")
        self.history = [point("2026-10-07T00:00:00Z"), point("2026-10-07T08:00:00Z"),
                        point("2026-10-08T10:00:00Z", 80_000)]
        self.policy = dict(notify_mode="deal", min_drop_pct=10, min_saving_cents=10_000,
                           min_review_rating=4, min_review_count=20)

    def assess(self, offer=None, history=None, policy=None):
        return assess(self.offer | (offer or {}), self.history if history is None else history,
                      self.policy | (policy or {}), now=NOW)

    def test_sustained_recent_drop_has_explainable_amount_and_rating(self):
        result = self.assess()
        self.assertEqual(result["kind"], "deal")
        self.assertEqual(result["baseline_cents"], 100_000)
        self.assertEqual(result["saving_cents"], 20_000)
        self.assertEqual(result["drop_pct"], 20)
        self.assertEqual(result["rating_5"], 4.5)
        self.assertEqual(result["drop_observed_at"], "2026-10-08T10:00:00Z")

    def test_baseline_needs_two_observations_at_least_six_hours_apart(self):
        low = self.history[-1]
        for history in ([self.history[0], low],
                        [point("2026-10-07T00:00:00Z"), point("2026-10-07T00:00:00Z"), low],
                        [point("2026-10-07T00:00:00Z"), point("2026-10-07T05:59:59Z"), low]):
            with self.subTest(history=history):
                self.assertIsNone(self.assess(history=history))
        self.assertIsNotNone(self.assess(history=[point("2026-10-07T00:00:00Z"),
                                                  point("2026-10-07T06:00:00Z"), low]))

    def test_repeated_low_price_preserves_first_observation_and_expiry(self):
        history = self.history[:2] + [point("2026-10-08T08:00:00Z", 80_000), self.history[-1]]
        self.assertEqual(self.assess(history=history)["drop_observed_at"], "2026-10-08T08:00:00Z")
        baseline = [point("2026-10-04T00:00:00Z"), point("2026-10-04T08:00:00Z")]
        at_boundary = baseline + [point("2026-10-05T12:00:00Z", 80_000), self.history[-1]]
        self.assertIsNotNone(self.assess(history=at_boundary))
        too_old = baseline + [point("2026-10-05T11:59:59Z", 80_000), self.history[-1]]
        self.assertIsNone(self.assess(history=too_old))

    def test_wrong_currency_rebound_and_spike_are_not_supported_deals(self):
        self.assertIsNone(self.assess(history=[point("2026-10-07T00:00:00Z", currency="USD"),
                                               point("2026-10-07T08:00:00Z", currency="USD"), self.history[-1]]))
        rebound = self.history + [point("2026-10-08T11:00:00Z", 90_000)]
        self.assertIsNone(self.assess(offer={"price_cents": 90_000}, history=rebound))
        spike = [point("2026-10-06T00:00:00Z", 80_000), point("2026-10-07T08:00:00Z"), self.history[-1]]
        self.assertIsNone(self.assess(history=spike))
        self.assertIsNone(self.assess(history=[]))

    def test_rating_requires_confirmed_match_enough_reviews_and_quality(self):
        for changes in ({"review_match_status": "ambiguous"}, {"review_match_status": None},
                        {"review_count": 19}, {"review_rating": 3.9},
                        {"review_rating": None}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.assess(offer=changes))
        self.assertIsNotNone(self.assess(offer={"review_rating": 9, "review_scale": 10}))

    def test_stale_past_departure_stop_sale_and_non_eur_are_excluded_in_every_mode(self):
        for changes in ({"fetched_at": "2026-10-08T05:59:59Z"},
                        {"fetched_at": "2026-10-08T12:01:00Z"},
                        {"date_start": "2026-10-07"}, {"stop_sale": "true"},
                        {"currency": "USD"}, {"price_cents": 0}):
            for mode in ("deal", "budget", "both"):
                with self.subTest(changes=changes, mode=mode):
                    self.assertIsNone(self.assess(offer=changes, policy={"notify_mode": mode, "budget_max": 1000}))

    def test_waavo_is_budget_only_until_room_identity_is_repaired(self):
        self.assertIsNone(self.assess(offer={"source": "waavo"}))
        for mode in ("budget", "both"):
            evidence = self.assess(offer={"source": "waavo"}, history=[],
                                   policy={"notify_mode": mode, "budget_max": 1000})
            self.assertEqual(evidence["kind"], "budget")
            self.assertNotIn("saving_cents", evidence)
        self.assertIsNone(self.assess(offer={"source": "waavo"}, policy={"notify_mode": "budget", "budget_max": 700}))

    def test_budget_fallback_never_claims_a_discount(self):
        result = self.assess(history=[], offer={"review_match_status": "ambiguous"},
                             policy={"notify_mode": "both", "budget_max": 1000})
        self.assertEqual(result["kind"], "budget")
        self.assertEqual(result["budget_cents"], 100_000)
        self.assertNotIn("drop_pct", result)
        self.assertIsNone(self.assess(policy={"min_saving_cents": 20_001}))
        self.assertIsNone(self.assess(policy={"min_drop_pct": 20.1}))


if __name__ == "__main__":
    unittest.main()
