"""Subscription evaluation uses only a fresh database in a temporary directory."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError

from tourfinder import db, queries, subscriptions


NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def clear_engines():
    for engine in db._engines.values():
        engine.dispose()
    db._engines.clear()


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        clear_engines()
        self.temp = tempfile.TemporaryDirectory(prefix="tourfinder-subscription-tests-")
        self.addCleanup(self.temp.cleanup)
        # Explicit temporary URL wins even if the developer has a production
        # DATABASE_URL set. Never open the default data/tourfinder.db.
        url = "sqlite:///" + (Path(self.temp.name) / "fixture.db").as_posix()
        self.env = patch.dict(os.environ, {"DATABASE_URL": url, "TELEGRAM_ALLOWED_USER_IDS": "42"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(clear_engines)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.time = patch.object(subscriptions, "datetime", FixedDatetime)
        self.time.start()
        self.addCleanup(self.time.stop)
        self.fresh = patch.object(queries, "_fresh_cutoff", return_value="2026-10-06T12:00:00Z")
        self.fresh.start()
        self.addCleanup(self.fresh.stop)
        self.counter = 0
        self.conn.execute("INSERT INTO hotels(source,source_hotel_id,name,category,country_id) VALUES ('joinup','h1','Hotel','4','c_8')")
        self.conn.execute("""INSERT INTO hotel_reviews(source,source_hotel_id,platform,rating,rating_scale,reviews_count,match_status,fetched_at)
                           VALUES ('joinup','h1','google',4.5,5,100,'ok','2026-10-08T10:00:00Z')""")
        self.conn.commit()

    def offer(self, prices=None, *, source="joinup", price=80_000):
        self.counter += 1
        if source != "joinup":
            self.conn.execute("INSERT INTO hotels(source,source_hotel_id,name) VALUES (:source,'h1','Hotel') ON CONFLICT(source,source_hotel_id) DO NOTHING", {"source": source})
        offer_id = self.conn.execute("""INSERT INTO offers(source,source_hotel_id,origin_id,date_start,nights,board_code,
                        room_code,room_placement,pax_adl,pax_chd,children_ages,first_seen_at,last_seen_at)
                        VALUES (:source,'h1','RIX','2026-10-20',7,'AI',:room,'2AD',2,0,'',
                        '2026-10-01T00:00:00Z','2026-10-08T10:00:00Z') RETURNING id""",
                        {"source": source, "room": str(self.counter)}).fetchone()["id"]
        if prices is None:
            prices = [("2026-10-07T00:00:00Z", 100_000), ("2026-10-07T08:00:00Z", 100_000),
                      ("2026-10-08T10:00:00Z", price)]
        for when, value in prices:
            self.snapshot(offer_id, when, value)
        return offer_id

    def snapshot(self, offer_id, when, price):
        self.conn.execute("INSERT INTO price_snapshots(offer_id,fetched_at,price_cents,currency,is_hot) VALUES (:id,:when,:price,'EUR',0)",
                          {"id": offer_id, "when": when, "price": price})

    def subscription(self, *, mode="deal", owner="42", enabled=1, budget=None):
        filters = dict(date_from="2026-10-10", date_till="2026-10-31", adults=2,
                       children_ages="", nights_min=1, nights_max=30)
        if budget is not None:
            filters["budget_max"] = budget
        sub_id = self.conn.execute("""INSERT INTO subscriptions(name,filters,enabled,created_at,owner_id,notify_mode)
                            VALUES ('Watch',:filters,:enabled,'2026-10-08T00:00:00Z',:owner,:mode) RETURNING id""",
                            {"filters": json.dumps(filters), "enabled": enabled, "owner": owner, "mode": mode}).fetchone()["id"]
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM subscriptions WHERE id=:id", {"id": sub_id}).fetchone())

    def count(self, table):
        return self.conn.execute("SELECT count(*) FROM " + table).scalar()

    def test_repeated_evaluation_creates_one_explained_alert_and_outbox_row(self):
        offer = self.offer()
        sub = self.subscription()
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 1)
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 0)
        self.assertEqual(self.count("alerts"), 1)
        self.assertEqual(self.count("telegram_deliveries"), 1)
        alert = self.conn.execute("SELECT * FROM alerts").fetchone()
        evidence = json.loads(alert["evidence"])
        self.assertEqual(alert["offer_id"], offer)
        self.assertEqual(alert["reason"], "price_drop")
        self.assertEqual(evidence["saving_cents"], 20_000)
        delivery = self.conn.execute("SELECT * FROM telegram_deliveries").fetchone()
        self.assertEqual((delivery["alert_id"], delivery["status"]), (alert["id"], "pending"))

    def test_budget_alert_only_repeats_at_a_new_lower_price(self):
        offer = self.offer(prices=[("2026-10-08T10:00:00Z", 80_000)], source="waavo")
        sub = self.subscription(mode="budget", budget=1000)
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 1)
        self.snapshot(offer, "2026-10-08T11:00:00Z", 70_000)
        self.conn.commit()
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 1)
        self.snapshot(offer, "2026-10-08T11:30:00Z", 75_000)
        self.conn.commit()
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 0)
        self.assertEqual(self.count("alerts"), 2)
        evidence = [json.loads(row["evidence"]) for row in self.conn.execute("SELECT evidence FROM alerts")]
        self.assertTrue(all(item["kind"] == "budget" and "drop_pct" not in item for item in evidence))

    def test_unowned_unauthorized_and_disabled_subscriptions_do_not_notify(self):
        self.offer()
        for owner, enabled in ((None, 1), ("999", 1), ("42", 0)):
            sub = self.subscription(owner=owner, enabled=enabled)
            self.assertEqual(subscriptions.evaluate(self.conn, sub), 0)
        self.assertEqual(subscriptions.evaluate_all(self.conn), 0)
        self.assertEqual(self.count("alerts"), 0)
        self.assertEqual(self.count("telegram_deliveries"), 0)

    def test_uncertain_review_and_waavo_identity_cannot_produce_deal_alerts(self):
        self.offer(source="waavo")
        self.offer()
        self.conn.execute("UPDATE hotel_reviews SET match_status='ambiguous'")
        sub = self.subscription()
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 0)
        self.assertEqual(self.count("alerts"), 0)

    def test_alert_and_outbox_roll_back_together_on_enqueue_failure(self):
        self.offer()
        sub = self.subscription()
        self.conn.execute("CREATE TRIGGER reject_test_delivery BEFORE INSERT ON telegram_deliveries BEGIN SELECT RAISE(ABORT,'test delivery failure'); END")
        self.conn.commit()
        with self.assertRaisesRegex(IntegrityError, "test delivery failure"):
            subscriptions.evaluate(self.conn, sub)
        observer = db.DB(self.conn.engine)
        try:
            # Another connection must not see the alert without its outbox
            # row, even before the failed evaluator's transaction rolls back.
            self.assertEqual(observer.execute("SELECT count(*) FROM alerts").scalar(), 0)
            self.assertEqual(observer.execute("SELECT count(*) FROM telegram_deliveries").scalar(), 0)
        finally:
            observer.close()
        self.conn.rollback()
        self.assertEqual(self.count("alerts"), 0)
        self.assertEqual(self.count("telegram_deliveries"), 0)

    def test_pruned_baseline_anchor_before_fourteen_days_is_preserved(self):
        self.offer(prices=[("2026-09-18T00:00:00Z", 100_000),
                           ("2026-10-07T08:00:00Z", 100_000),
                           ("2026-10-08T10:00:00Z", 80_000)])
        sub = self.subscription()
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 1)
        evidence = json.loads(self.conn.execute("SELECT evidence FROM alerts").fetchone()["evidence"])
        self.assertEqual(evidence["baseline_from"], "2026-09-18T00:00:00Z")

    def test_paged_sweep_reaches_deal_after_first_five_hundred_non_deals(self):
        for _ in range(500):
            self.offer(prices=[("2026-10-08T10:00:00Z", 50_000)])
        qualifying = self.offer()
        sub = self.subscription()
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 0)
        offset = self.conn.execute("SELECT evaluation_offset FROM subscriptions WHERE id=:id", {"id": sub["id"]}).scalar()
        self.assertEqual(offset, 500)
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 1)
        self.assertEqual(self.conn.execute("SELECT offer_id FROM alerts").scalar(), qualifying)
        self.assertEqual(self.conn.execute("SELECT evaluation_offset FROM subscriptions WHERE id=:id", {"id": sub["id"]}).scalar(), 0)
        self.assertEqual(self.count("telegram_deliveries"), 1)

    def test_meal_category_is_applied_during_saved_search_evaluation(self):
        breakfast = self.offer(price=50_000)
        selected = self.offer(price=80_000)
        ultra = self.offer(price=70_000)
        self.conn.execute("UPDATE offers SET board_code='BB',board_name='Breakfast' WHERE id=:id", {"id": breakfast})
        self.conn.execute("UPDATE offers SET board_code='SOFTAI',board_name='Soft all inclusive' WHERE id=:id", {"id": selected})
        self.conn.execute("UPDATE offers SET board_code='UAI',board_name='Ultra all inclusive' WHERE id=:id", {"id": ultra})
        sub = self.subscription(mode="budget", budget=1000)
        filters = json.loads(sub["filters"])
        filters.update(board_categories="AI", boards="SOFTAI,UAI")
        self.conn.execute("UPDATE subscriptions SET filters=:filters WHERE id=:id",
                          {"id": sub["id"], "filters": json.dumps(filters)})
        self.conn.commit()
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 1)
        self.assertEqual([row["offer_id"] for row in self.conn.execute("SELECT offer_id FROM alerts")], [selected])
        self.assertEqual(self.count("telegram_deliveries"), 1)

    def test_persisted_approved_user_can_evaluate_but_revocation_stops_new_alerts(self):
        offer = self.offer()
        self.conn.execute("""INSERT INTO telegram_access_requests(user_id,status,first_name,requested_at)
            VALUES ('789','approved','Fixture','2026-10-08T00:00:00Z')""")
        sub = self.subscription(owner="789")
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 1)
        self.conn.execute("UPDATE telegram_access_requests SET status='denied' WHERE user_id='789'")
        self.snapshot(offer, "2026-10-08T11:00:00Z", 60_000)
        self.conn.commit()
        self.assertEqual(subscriptions.evaluate(self.conn, sub), 0)
        self.assertEqual(self.count("alerts"), 1)

    def cursor_state(self):
        values = {row["name"]: row["last_id"] for row in self.conn.execute(
            "SELECT name,last_id FROM id_counters")}
        self.conn.commit()
        return values[subscriptions._CURSOR_NAME], values[subscriptions._VERSION_NAME]

    def evaluate_one_page(self):
        with patch.object(subscriptions.time, "monotonic", side_effect=[0, 2]):
            return subscriptions.evaluate_all(self.conn, deadline=1)

    def test_deadline_resumes_three_subscriptions_then_wraps_without_duplicate_alerts(self):
        self.offer()
        subs = [self.subscription() for _ in range(3)]
        with patch("requests.post", side_effect=AssertionError("HTTP is forbidden during evaluation")):
            for index, sub in enumerate(subs, 1):
                self.assertEqual(self.evaluate_one_page(), 1)
                self.assertEqual(self.cursor_state(), (sub["id"], index))
            self.assertEqual(self.evaluate_one_page(), 0)
        self.assertEqual(self.cursor_state(), (subs[0]["id"], 4))
        self.assertEqual({row["subscription_id"] for row in self.conn.execute("SELECT subscription_id FROM alerts")},
                         {sub["id"] for sub in subs})
        self.assertEqual(self.count("alerts"), 3)
        self.assertEqual(self.count("telegram_deliveries"), 3)
        self.assertTrue(all(row["status"] == "pending" for row in self.conn.execute("SELECT status FROM telegram_deliveries")))

    def test_cursor_wraps_past_deleted_and_revoked_subscriptions(self):
        self.offer()
        first = self.subscription()
        deleted = self.subscription()
        revoked = self.subscription(owner="789")
        last = self.subscription()
        self.conn.execute("""INSERT INTO telegram_access_requests(user_id,status,first_name,requested_at)
            VALUES ('789','denied','Fixture','2026-10-08T00:00:00Z')""")
        self.conn.commit()
        self.assertEqual(self.evaluate_one_page(), 1)
        self.conn.execute("DELETE FROM subscriptions WHERE id=:id", {"id": deleted["id"]})
        self.conn.commit()
        self.assertEqual(self.evaluate_one_page(), 0)  # Committed access skip advances too.
        self.assertEqual(self.cursor_state(), (revoked["id"], 2))
        self.assertEqual(self.evaluate_one_page(), 1)
        self.assertEqual(self.cursor_state(), (last["id"], 3))
        self.assertEqual(self.evaluate_one_page(), 0)
        self.assertEqual(self.cursor_state(), (first["id"], 4))
        self.assertEqual({row["subscription_id"] for row in self.conn.execute("SELECT subscription_id FROM alerts")},
                         {first["id"], last["id"]})

    def test_disabled_between_listing_and_evaluation_is_a_committed_cursor_skip(self):
        first, second = self.subscription(), self.subscription()
        original = subscriptions.evaluate

        def disable_then_evaluate(conn, sub):
            conn.execute("UPDATE subscriptions SET enabled=0 WHERE id=:id", {"id": sub["id"]})
            conn.commit()
            return original(conn, sub)

        with patch.object(subscriptions, "evaluate", side_effect=disable_then_evaluate):
            self.assertEqual(self.evaluate_one_page(), 0)
        self.assertEqual(self.cursor_state(), (first["id"], 1))
        self.assertEqual(self.evaluate_one_page(), 0)
        self.assertEqual(self.cursor_state(), (second["id"], 2))

    def test_cursor_cas_rejects_stale_peer_even_after_id_wraps_back(self):
        self.assertEqual(subscriptions._evaluation_cursor(self.conn), (0, 0))
        peer = db.DB(self.conn.engine)
        try:
            self.assertEqual(subscriptions._advance_evaluation_cursor(self.conn, 10, 0), 1)
            self.assertEqual(subscriptions._evaluation_cursor(peer), (10, 1))
            self.assertEqual(subscriptions._advance_evaluation_cursor(peer, 20, 1), 2)
            self.assertEqual(subscriptions._advance_evaluation_cursor(peer, 10, 2), 3)
            self.assertIsNone(subscriptions._advance_evaluation_cursor(self.conn, 99, 1))
            self.assertEqual(self.cursor_state(), (10, 3))
            self.assertFalse(self.conn._conn.in_transaction())
            self.assertFalse(peer._conn.in_transaction())
        finally:
            peer.close()

    def test_peer_cursor_progress_stops_stale_sweep_after_its_committed_page(self):
        self.offer()
        first, second, third = [self.subscription() for _ in range(3)]
        original = subscriptions.evaluate
        checked = []

        def evaluate_with_peer(conn, sub):
            checked.append(sub["id"])
            created = original(conn, sub)
            peer = db.DB(conn.engine)
            try:
                self.assertEqual(subscriptions._advance_evaluation_cursor(peer, third["id"], 0), 1)
            finally:
                peer.close()
            return created

        with patch.object(subscriptions, "evaluate", side_effect=evaluate_with_peer):
            self.assertEqual(subscriptions.evaluate_all(self.conn), 1)
        self.assertEqual(checked, [first["id"]])
        self.assertEqual(self.cursor_state(), (third["id"], 1))
        self.assertEqual(self.count("alerts"), 1)  # The successfully committed page is retained.

    def test_failed_page_propagates_and_does_not_advance_cursor(self):
        self.offer()
        self.subscription()
        with patch.object(subscriptions, "assess", side_effect=ValueError("fixture policy failure")):
            with self.assertRaisesRegex(ValueError, "fixture policy failure"):
                subscriptions.evaluate_all(self.conn)
        self.conn.rollback()
        self.assertEqual(self.cursor_state(), (0, 0))
        self.assertEqual(self.count("alerts"), 0)
        self.assertEqual(self.count("telegram_deliveries"), 0)

    def test_cursor_publish_failure_replays_safely_after_committed_evaluation(self):
        self.offer()
        sub = self.subscription()
        with patch.object(subscriptions, "_advance_evaluation_cursor", side_effect=RuntimeError("fixture cursor failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture cursor failure"):
                subscriptions.evaluate_all(self.conn)
        self.assertEqual(self.cursor_state(), (0, 0))
        self.assertEqual(self.count("alerts"), 1)
        self.assertEqual(self.count("telegram_deliveries"), 1)
        self.assertEqual(subscriptions.evaluate_all(self.conn), 0)
        self.assertEqual(self.cursor_state(), (sub["id"], 1))
        self.assertEqual(self.count("alerts"), 1)

    def test_cursor_and_revision_update_roll_back_together(self):
        subscriptions._evaluation_cursor(self.conn)
        self.conn.execute("""CREATE TRIGGER reject_cursor BEFORE UPDATE ON id_counters
            WHEN NEW.name='subscription_evaluation_cursor_v1'
            BEGIN SELECT RAISE(ABORT, 'fixture cursor write failure'); END""")
        self.conn.commit()
        with self.assertRaises(IntegrityError):
            subscriptions._advance_evaluation_cursor(self.conn, 7, 0)
        self.assertEqual(self.cursor_state(), (0, 0))

    def test_cursor_initialization_is_idempotent_preserves_id_counters_and_releases_transactions(self):
        self.conn.execute("INSERT INTO id_counters(name,last_id) VALUES ('alerts',600),('subscriptions',800)")
        self.conn.commit()
        self.assertEqual(subscriptions._evaluation_cursor(self.conn), (0, 0))
        subscriptions._advance_evaluation_cursor(self.conn, 7, 0)
        self.assertEqual(subscriptions._evaluation_cursor(self.conn), (7, 1))
        self.assertFalse(self.conn._conn.in_transaction())
        values = {row["name"]: row["last_id"] for row in self.conn.execute(
            "SELECT name,last_id FROM id_counters WHERE name IN ('alerts','subscriptions')")}
        self.assertEqual(values, {"alerts": 600, "subscriptions": 800})

    def test_empty_or_already_exhausted_sweep_leaves_no_transaction_open(self):
        self.assertEqual(subscriptions.evaluate_all(self.conn), 0)
        self.assertFalse(self.conn._conn.in_transaction())
        self.subscription()
        with patch.object(subscriptions.time, "monotonic", return_value=2):
            self.assertEqual(subscriptions.evaluate_all(self.conn, deadline=1), 0)
        self.assertFalse(self.conn._conn.in_transaction())
        self.assertEqual(self.cursor_state(), (0, 0))


if __name__ == "__main__":
    unittest.main()
