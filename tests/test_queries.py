"""Query regressions against disposable, in-memory SQLite only.

This module deliberately does not import tourfinder.db: DATABASE_URL and the
user's data/tourfinder.db must never be consulted by these tests.
"""
import sqlite3
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from tourfinder import deals, queries


SCHEMA = """
CREATE TABLE hotels (
    source TEXT, source_hotel_id TEXT, name TEXT, category TEXT,
    country_id TEXT, country_name TEXT, city_name TEXT, photo_url TEXT,
    PRIMARY KEY (source, source_hotel_id)
);
CREATE TABLE offers (
    id INTEGER PRIMARY KEY, source TEXT, source_hotel_id TEXT,
    origin_id TEXT, origin_name TEXT, date_start TEXT, date_end TEXT,
    nights INTEGER, board_code TEXT, board_name TEXT, room_code TEXT,
    room_name TEXT, room_placement TEXT, pax_adl INTEGER, pax_chd INTEGER,
    children_ages TEXT, operator TEXT, link TEXT,
    first_seen_at TEXT, last_seen_at TEXT
);
CREATE INDEX idx_offers_date ON offers(date_start, nights);
CREATE TABLE price_snapshots (
    id INTEGER PRIMARY KEY, offer_id INTEGER, run_id INTEGER,
    fetched_at TEXT, price_cents INTEGER, currency TEXT, is_hot INTEGER,
    availability TEXT, stop_sale TEXT, operator_avg_price_cents INTEGER
);
CREATE INDEX idx_snapshots_offer ON price_snapshots(offer_id, fetched_at);
CREATE INDEX idx_snapshots_latest ON price_snapshots(offer_id, fetched_at, id);
CREATE TABLE hotel_reviews (
    id INTEGER PRIMARY KEY, source TEXT, source_hotel_id TEXT,
    platform TEXT, rating REAL, rating_scale REAL, reviews_count INTEGER, url TEXT,
    match_status TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX idx_reviews_hotel ON hotel_reviews(source, source_hotel_id);
"""


class RecordingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.query = None
        self.params = None

    def execute(self, query, params=None):
        self.query, self.params = query, params or {}
        return self.connection.execute(query, self.params)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.sqlite = sqlite3.connect(":memory:")
        self.sqlite.row_factory = sqlite3.Row
        self.sqlite.executescript(SCHEMA)
        self.conn = RecordingConnection(self.sqlite)
        self.fresh = patch.object(queries, "_fresh_cutoff", return_value="2026-10-06T12:00:00Z")
        self.fresh.start()
        self.addCleanup(self.fresh.stop)
        self.addCleanup(self.sqlite.close)
        self.filters = dict(date_from="2026-10-10", date_till="2026-10-31",
                            adults=2, children_ages=None, nights_min=1,
                            nights_max=30, budget_max=None, boards=None,
                            countries=None, only_hot=False)
        self.hotel("h1")

    def hotel(self, identifier, *, source="joinup", category="4+", country="c_8"):
        self.sqlite.execute("INSERT INTO hotels VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (source, identifier, identifier, category, country,
                             "Turkey", "Antalya", "https://example.test/photo"))

    def offer(self, **changes):
        values = dict(source="joinup", source_hotel_id="h1", origin_id="RIX",
                      origin_name="Riga", date_start="2026-10-20", date_end="2026-10-27",
                      nights=7, board_code="AI", board_name="All inclusive",
                      room_code="standard", room_name="Standard", room_placement="2AD",
                      pax_adl=2, pax_chd=0, children_ages="", operator="joinup",
                      link="https://example.test/offer", first_seen_at="2026-10-01T00:00:00Z",
                      last_seen_at="2026-10-08T10:00:00Z")
        values.update(changes)
        fields = ",".join(values)
        markers = ",".join("?" for _ in values)
        return self.sqlite.execute(f"INSERT INTO offers ({fields}) VALUES ({markers})",
                                   tuple(values.values())).lastrowid

    def snapshot(self, offer, price, when="2026-10-08T10:00:00Z", *, hot=0,
                 currency="EUR", stop_sale=None):
        return self.sqlite.execute(
            """INSERT INTO price_snapshots(offer_id, fetched_at, price_cents,
               currency, is_hot, stop_sale) VALUES (?, ?, ?, ?, ?, ?)""",
            (offer, when, price, currency, hot, stop_sale)).lastrowid

    def search(self, **changes):
        return queries.search_offers(self.conn, **(self.filters | changes))

    def grouped(self, **changes):
        return queries.search_hotels_grouped(self.conn, **(self.filters | changes))

    def drops(self, **changes):
        args = dict(since="2026-10-05T12:00:00Z", today="2026-10-08")
        return queries.price_drops(self.conn, **(args | changes))

    def test_latest_orders_by_timestamp_then_id_before_price_and_hot_filters(self):
        offer = self.offer(pax_chd=1, children_ages="7")
        self.snapshot(offer, 80_000, hot=1)
        self.snapshot(offer, 120_000, stop_sale="closed")
        # A backfilled older observation has a higher id; it must not win.
        self.snapshot(offer, 60_000, "2026-10-07T10:00:00Z", hot=1)
        result = self.search(children_ages="7")[0]
        self.assertEqual(result["price_cents"], 120_000)
        self.assertEqual(result["snapshots_count"], 3)
        self.assertEqual(result["min_seen_cents"], 60_000)
        self.assertEqual(result["max_seen_cents"], 120_000)
        self.assertAlmostEqual(result["avg_seen_cents"], 260_000 / 3)
        self.assertEqual((result["source"], result["pax_chd"], result["children_ages"],
                          result["stop_sale"]), ("joinup", 1, "7", "closed"))
        self.assertEqual(self.search(children_ages="7", budget_max=1000), [])
        self.assertEqual(self.search(children_ages="7", only_hot=True), [])

    def test_verified_room_identity_reaches_search_grouping_detail_and_deal_assessment(self):
        self.hotel("h1", source="waavo")
        code = "wv2:" + "ab" * 32
        offer = self.offer(source="waavo", room_code=code)
        history = []
        for when, price in (("2026-10-07T00:00:00Z", 100_000),
                            ("2026-10-07T08:00:00Z", 100_000),
                            ("2026-10-08T10:00:00Z", 80_000)):
            self.snapshot(offer, price, when)
            history.append({"fetched_at": when, "price_cents": price, "currency": "EUR"})
        self.sqlite.execute("""INSERT INTO hotel_reviews(source,source_hotel_id,platform,
                            rating,rating_scale,reviews_count,match_status)
                            VALUES ('waavo','h1','tripadvisor',4.5,5,100,'ok')""")
        for result in (self.search()[0], self.grouped()[0], queries.offer_detail(self.conn, offer)):
            with self.subTest(query_result=result["offer_id"]):
                self.assertEqual(result["room_code"], code)
                evidence = deals.assess(result, history, {"notify_mode": "deal"},
                                        now=datetime(2026, 10, 8, 12, tzinfo=timezone.utc))
                self.assertEqual(evidence["kind"], "deal")
                self.assertEqual(evidence["saving_cents"], 20_000)
        # The query must return legacy identity as-is, never upgrade old rows.
        self.sqlite.execute("UPDATE offers SET room_code='' WHERE id=?", (offer,))
        legacy = self.search()[0]
        self.assertEqual(legacy["room_code"], "")
        self.assertIsNone(deals.assess(legacy, history, {"notify_mode": "deal"},
                                      now=datetime(2026, 10, 8, 12, tzinfo=timezone.utc)))

    def test_every_offer_and_hotel_filter_is_preserved(self):
        self.hotel("low", category="3")
        self.hotel("text", category="HV1")
        self.hotel("other", country="c_9")
        common = dict(pax_chd=2, children_ages="6,8")
        good = self.offer(**common)
        self.snapshot(good, 90_000, hot=1)
        for changes in (
            {"pax_adl": 3}, {"children_ages": "6,7"}, {"nights": 14},
            {"date_start": "2026-11-01"}, {"board_code": "BB"},
            {"last_seen_at": "2026-10-05T10:00:00Z"},
            {"source_hotel_id": "low"}, {"source_hotel_id": "text"},
            {"source_hotel_id": "other"},
        ):
            self.snapshot(self.offer(**(common | changes)), 50_000, hot=1)
        self.snapshot(self.offer(**common), 110_000, hot=1)
        self.snapshot(self.offer(**common), 50_000, hot=0)
        filters = dict(children_ages="8,6", nights_min=6, nights_max=8,
                       budget_max=1000, boards=" AI, HB ", countries="c_8,c_50",
                       stars_min=4, only_hot=True)
        for query in (self.search, self.grouped):
            with self.subTest(query=query.__name__):
                results = query(**filters)
                self.assertEqual([r["offer_id"] for r in results], [good])
                self.assertEqual(results[0]["children_ages"], "6,8")
        self.assertEqual(self.search(**filters, hotel_id="other"), [])

    def test_limit_does_not_starve_late_budget_or_hot_matches(self):
        for _ in range(525):
            offer = self.offer()
            self.snapshot(offer, 50_000, "2026-10-07T00:00:00Z", hot=1)
            self.snapshot(offer, 300_000, hot=0)
        late = self.offer()
        self.snapshot(late, 80_000, hot=1)
        for query in (self.search, self.grouped):
            self.assertEqual([r["offer_id"] for r in query(budget_max=1000, only_hot=True, limit=1)], [late])

    def test_grouping_limits_hotels_and_keeps_all_variant_statistics(self):
        for i in range(20):
            offer = self.offer(date_start=f"2026-10-{10 + i:02d}")
            self.snapshot(offer, 10_000 + i * 100)
        self.hotel("h2")
        second = self.offer(source_hotel_id="h2")
        self.snapshot(second, 20_000)
        results = self.grouped(limit=2)
        self.assertEqual([r["source_hotel_id"] for r in results], ["h1", "h2"])
        first = results[0]
        self.assertEqual(first["variants"], 20)
        self.assertEqual((first["variants_min_cents"], first["variants_max_cents"]), (10_000, 11_900))
        self.assertEqual((first["variants_date_from"], first["variants_date_till"]), ("2026-10-10", "2026-10-29"))
        self.hotel("h1", source="waavo")
        other_source = self.offer(source="waavo")
        self.snapshot(other_source, 15_000)
        self.assertEqual([(r["source"], r["source_hotel_id"]) for r in self.grouped(limit=3)],
                         [("joinup", "h1"), ("waavo", "h1"), ("joinup", "h2")])

    def test_hotel_variants_can_be_scoped_to_their_source(self):
        self.hotel("h1", source="waavo")
        direct = self.offer()
        aggregator = self.offer(source="waavo")
        self.snapshot(direct, 90_000)
        self.snapshot(aggregator, 80_000)
        for query in (self.search, self.grouped):
            with self.subTest(query=query.__name__):
                self.assertEqual([r["offer_id"] for r in query(hotel_id="h1", source="joinup")], [direct])
                self.assertEqual([r["offer_id"] for r in query(hotel_id="h1", source="waavo")], [aggregator])
                self.assertEqual(query(hotel_id="h1", source="' OR 1=1 --"), [])

    def test_board_categories_cover_standard_codes_and_source_fallback_names(self):
        examples = [
            ("RO", "Room only", "RO"), ("BB", "Breakfast", "BB"),
            ("HB", "Half board", "HB"), ("FB", "Full board", "FB"),
            ("AI", "All inclusive", "AI"), ("UAI", "Ultra all inclusive", "UAI"),
            ("SOFTAI", "Soft all inclusive", "AI"),
            ("OT", "SELF CATERING", "RO"), ("", "SELF CATERING", "RO"),
            ("", "Super UAI", "UAI"), ("ZZ", "Unspecified package", "OTHER"),
            # Explicit standard codes win over a conflicting source description.
            ("BB", "Ultra all inclusive", "BB"), ("AI", "Super UAI", "AI"),
        ]
        expected = {}
        for code, name, category in examples:
            offer = self.offer(board_code=code, board_name=name)
            self.snapshot(offer, 80_000)
            expected[offer] = category
        rows = self.search()
        self.assertEqual({row["offer_id"]: row["board_category"] for row in rows}, expected)
        self.assertTrue(all(isinstance(row["board_label"], str) and row["board_label"].strip() for row in rows))
        for category in {item[2] for item in examples}:
            with self.subTest(category=category):
                grouped = self.grouped(board_categories=category)
                self.assertEqual(len(grouped), 1)
                self.assertEqual(grouped[0]["board_category"], category)
                self.assertEqual(grouped[0]["variants"], sum(value == category for value in expected.values()))

    def test_board_category_filter_precedes_limit_and_grouped_variant_statistics(self):
        for _ in range(8):
            self.snapshot(self.offer(board_code="BB", board_name="Breakfast"), 10_000)
        selected = self.offer(board_code="SOFTAI", board_name="Soft all inclusive")
        self.snapshot(selected, 80_000)
        self.snapshot(self.offer(board_code="AI", date_start="2026-10-21"), 90_000)
        self.hotel("h2")
        self.snapshot(self.offer(source_hotel_id="h2", board_code="AI"), 95_000)
        for query in (self.search, self.grouped):
            with self.subTest(query=query.__name__):
                self.assertEqual([row["offer_id"] for row in query(board_categories="AI", limit=1)], [selected])
        grouped = self.grouped(board_categories="AI", limit=1)[0]
        self.assertEqual(grouped["variants"], 2)
        self.assertEqual((grouped["variants_min_cents"], grouped["variants_max_cents"]), (80_000, 90_000))
        self.assertEqual((grouped["variants_date_from"], grouped["variants_date_till"]), ("2026-10-20", "2026-10-21"))

    def test_board_categories_intersect_legacy_raw_board_codes(self):
        soft = self.offer(board_code="SOFTAI")
        standard = self.offer(board_code="AI")
        breakfast = self.offer(board_code="BB", board_name="Breakfast")
        for offer, price in ((soft, 70_000), (standard, 80_000), (breakfast, 50_000)):
            self.snapshot(offer, price)
        for query in (self.search, self.grouped):
            with self.subTest(query=query.__name__):
                self.assertEqual([row["offer_id"] for row in query(boards="SOFTAI")], [soft])
                self.assertEqual([row["offer_id"] for row in query(board_categories="AI", boards="AI")], [standard])
                self.assertEqual(query(board_categories="AI", boards="BB"), [])
                self.assertEqual(query(board_categories=None), query())

    def test_offer_detail_uses_latest_timestamp_then_id_and_preserves_search_data(self):
        offer = self.offer(date_start="2999-10-20", date_end="2999-10-27", room_placement="2AD+1CH")
        self.snapshot(offer, 100_000)
        self.snapshot(offer, 80_000, hot=1, stop_sale="closed")
        self.snapshot(offer, 50_000, "2026-10-07T10:00:00Z")  # Backfill with higher id.
        searched = self.search(date_from="2999-10-10", date_till="2999-10-31")[0]
        detail = queries.offer_detail(self.conn, offer)
        self.assertEqual(detail["offer_id"], offer)
        self.assertEqual(detail["price_cents"], 80_000)
        self.assertEqual(detail["fetched_at"], "2026-10-08T10:00:00Z")
        self.assertEqual(detail["stop_sale"], "closed")
        self.assertEqual(detail["room_placement"], "2AD+1CH")
        self.assertEqual(detail["last_seen_at"], "2026-10-08T10:00:00Z")
        self.assertIs(detail["stale"], False)
        for key in ("source", "source_hotel_id", "hotel_name", "board_category", "board_label",
                    "snapshots_count", "avg_seen_cents", "min_seen_cents", "max_seen_cents"):
            self.assertEqual(detail[key], searched[key], key)

    def test_offer_detail_reads_old_offer_but_marks_it_stale(self):
        offer = self.offer(date_start="2999-10-20", last_seen_at="2026-10-01T00:00:00Z")
        self.snapshot(offer, 70_000)
        self.assertEqual(self.search(date_from="2999-10-01", date_till="2999-10-31"), [])
        detail = queries.offer_detail(self.conn, offer)
        self.assertEqual(detail["price_cents"], 70_000)
        self.assertIs(detail["stale"], True)

    def test_offer_detail_marks_old_snapshot_and_past_departure_stale(self):
        old_snapshot = self.offer(date_start="2999-10-20")
        self.snapshot(old_snapshot, 70_000, "2026-10-01T00:00:00Z")
        departed = self.offer(date_start="2000-01-01")
        self.snapshot(departed, 60_000)
        for offer in (old_snapshot, departed):
            with self.subTest(offer=offer):
                self.assertIs(queries.offer_detail(self.conn, offer)["stale"], True)

    def test_offer_detail_missing_offer_or_missing_history_is_absent(self):
        without_history = self.offer()
        self.assertIsNone(queries.offer_detail(self.conn, without_history))
        self.assertIsNone(queries.offer_detail(self.conn, 999_999))

    def test_price_per_night_changes_best_variant_and_hotel_order(self):
        short = self.offer(nights=3)
        long = self.offer(nights=9)
        self.snapshot(short, 60_000)
        self.snapshot(long, 90_000)
        self.hotel("h2")
        second = self.offer(source_hotel_id="h2", nights=10)
        self.snapshot(second, 80_000)
        self.assertEqual([r["offer_id"] for r in self.search(sort="price_per_night")], [second, long, short])
        grouped = self.grouped(sort="price_per_night")
        self.assertEqual([r["offer_id"] for r in grouped], [second, long])
        self.assertEqual(grouped[1]["variants_min_cents"], 60_000)
        # Unknown sort is never interpolated, and falls back to price.
        self.assertEqual(self.search(sort="price; DROP TABLE offers")[0]["offer_id"], short)

    def test_review_selection_uses_count_then_id_and_does_not_duplicate_offers(self):
        offer = self.offer()
        self.snapshot(offer, 90_000)
        for platform, rating, count in (("null", 5, None), ("small", 5, 10),
                                        ("tie-old", 4, 100), ("tie-new", 4.5, 100),
                                        ("no-rating", None, 1000)):
            self.sqlite.execute("INSERT INTO hotel_reviews(source,source_hotel_id,platform,rating,rating_scale,reviews_count) VALUES ('joinup','h1',?,?,5,?)",
                                (platform, rating, count))
        self.sqlite.execute("INSERT INTO hotel_reviews(source,source_hotel_id,platform,rating,rating_scale,reviews_count,match_status) VALUES ('joinup','h1','uncertain',5,5,10000,'ambiguous')")
        for query in (self.search, self.grouped):
            results = query()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["review_platform"], "tie-new")
            self.assertEqual(results[0]["review_rating"], 4.5)
            self.assertEqual(results[0]["review_match_status"], "ok")

    def test_unconfirmed_reviews_are_not_returned_as_trusted_ratings(self):
        self.snapshot(self.offer(), 90_000)
        self.sqlite.execute("INSERT INTO hotel_reviews(source,source_hotel_id,platform,rating,rating_scale,reviews_count,match_status) VALUES ('joinup','h1','uncertain',5,5,10000,'ambiguous')")
        for query in (self.search, self.grouped):
            row = query()[0]
            self.assertIsNone(row["review_rating"])
            self.assertIsNone(row["review_match_status"])

    def test_repeated_lower_price_retains_actual_recent_drop(self):
        offer = self.offer()
        self.snapshot(offer, 100_000, "2026-10-04T00:00:00Z")
        self.snapshot(offer, 80_000, "2026-10-07T00:00:00Z")
        self.snapshot(offer, 80_000, "2026-10-08T00:00:00Z")
        drop = self.drops()[0]
        self.assertEqual(drop["prev_price_cents"], 100_000)
        self.assertEqual(drop["prev_fetched_at"], "2026-10-04T00:00:00Z")
        self.assertEqual(drop["drop_observed_at"], "2026-10-07T00:00:00Z")
        self.assertEqual(drop["fetched_at"], "2026-10-08T00:00:00Z")
        self.assertEqual(drop["drop_cents"], 20_000)
        self.assertEqual(drop["snapshots_count"], 3)

    def test_new_poll_does_not_revive_an_old_drop(self):
        offer = self.offer()
        self.snapshot(offer, 100_000, "2026-10-01T00:00:00Z")
        self.snapshot(offer, 80_000, "2026-10-02T00:00:00Z")
        self.snapshot(offer, 80_000, "2026-10-08T00:00:00Z")
        self.assertEqual(self.drops(), [])
        self.assertEqual(self.drops(since=None)[0]["offer_id"], offer)

    def test_rebounds_currencies_and_unrelated_rooms_do_not_invent_discounts(self):
        rebound = self.offer()
        for when, price in (("06", 100_000), ("07", 80_000), ("08", 90_000)):
            self.snapshot(rebound, price, f"2026-10-{when}T00:00:00Z")
        currency = self.offer()
        self.snapshot(currency, 100_000, "2026-10-06T00:00:00Z")
        self.snapshot(currency, 80_000, "2026-10-07T00:00:00Z", currency="USD")
        self.snapshot(currency, 80_000, "2026-10-08T00:00:00Z", currency="USD")
        expensive_room = self.offer(room_code="suite")
        cheap_room = self.offer(room_code="economy")
        self.snapshot(expensive_room, 100_000)
        self.snapshot(cheap_room, 50_000)
        self.assertEqual(self.drops(), [])

    def test_drop_same_timestamp_ties_use_id_and_keep_first_lower_observation(self):
        offer = self.offer()
        self.snapshot(offer, 100_000)
        self.snapshot(offer, 80_000)
        self.snapshot(offer, 80_000)
        drop = self.drops()[0]
        self.assertEqual(drop["drop_cents"], 20_000)
        self.assertEqual(drop["drop_observed_at"], "2026-10-08T10:00:00Z")

    def test_drop_source_filter_precedes_ranking_and_limit(self):
        direct = self.offer()
        self.snapshot(direct, 100_000, "2026-10-07T00:00:00Z")
        self.snapshot(direct, 80_000)
        self.hotel("h1", source="waavo")
        aggregator = self.offer(source="waavo")
        self.snapshot(aggregator, 100_000, "2026-10-07T00:00:00Z")
        self.snapshot(aggregator, 10_000)
        self.assertEqual(self.drops(limit=1)[0]["offer_id"], aggregator)
        self.assertEqual(self.drops(source="joinup", limit=1)[0]["offer_id"], direct)
        self.assertEqual(self.drops(source="' OR 1=1 --", limit=1), [])

    def test_drop_filters_and_relative_sort_are_applied_before_limit(self):
        for changes in ({"pax_adl": 3}, {"children_ages": "7", "pax_chd": 1},
                        {"date_start": "2026-10-07"}, {"last_seen_at": "2026-10-01T00:00:00Z"}):
            offer = self.offer(**changes)
            self.snapshot(offer, 100_000, "2026-10-07T00:00:00Z")
            self.snapshot(offer, 10_000)
        smaller = self.offer()
        self.snapshot(smaller, 200_000, "2026-10-07T00:00:00Z")
        self.snapshot(smaller, 180_000)
        larger = self.offer()
        self.snapshot(larger, 100_000, "2026-10-07T00:00:00Z")
        self.snapshot(larger, 80_000)
        self.assertEqual([r["offer_id"] for r in self.drops(limit=1)], [larger])
        self.assertEqual(self.drops(limit=0), [])
        self.assertEqual(self.search(limit=0), [])
        self.assertEqual(self.grouped(limit=0), [])

    def test_equal_prices_have_stable_offer_order(self):
        first, second = self.offer(), self.offer()
        self.snapshot(second, 80_000)
        self.snapshot(first, 80_000)
        self.assertEqual([r["offer_id"] for r in self.search()], [first, second])
        self.assertEqual(self.grouped()[0]["offer_id"], first)

    def test_offset_pages_after_all_filters_with_stable_ties(self):
        excluded = self.offer()
        self.snapshot(excluded, 1_000, hot=0)
        matching = [self.offer() for _ in range(4)]
        for offer in reversed(matching):
            self.snapshot(offer, 80_000, hot=1)
        self.assertEqual([r["offer_id"] for r in self.search(only_hot=True, limit=2)], matching[:2])
        self.assertEqual([r["offer_id"] for r in self.search(only_hot=True, limit=2, offset=2)], matching[2:])
        self.assertEqual(self.search(only_hot=True, offset=4), [])
        self.assertEqual(self.search(only_hot=True, offset=-1), self.search(only_hot=True))
        self.assertEqual(self.grouped(only_hot=True)[0]["variants"], 4)

    def test_irrelevant_large_history_is_not_scanned_or_ranked(self):
        eligible = self.offer()
        self.snapshot(eligible, 100_000, "2026-10-07T00:00:00Z")
        self.snapshot(eligible, 80_000)
        irrelevant = self.offer(pax_adl=4)
        self.snapshot(irrelevant, 100_000)

        def instructions(query):
            steps = 0

            def tick():
                nonlocal steps
                steps += 1
                return 0

            self.sqlite.set_progress_handler(tick, 1)
            try:
                result = query()
            finally:
                self.sqlite.set_progress_handler(None, 0)
            self.assertEqual([r["offer_id"] for r in result], [eligible])
            return steps

        queries_to_check = (self.search, self.grouped, self.drops)
        before = [instructions(query) for query in queries_to_check]
        self.sqlite.executemany(
            "INSERT INTO price_snapshots(offer_id,fetched_at,price_cents,currency,is_hot) VALUES (?,'2026-10-08T10:00:00Z',100000,'EUR',0)",
            ((irrelevant,) for _ in range(50_000)))
        for query, baseline in zip(queries_to_check, before):
            with self.subTest(query=query.__name__):
                after = instructions(query)
                # Deterministic VM work, not a flaky wall-clock threshold.
                self.assertLess(after, baseline + 500)
                plan = self.sqlite.execute("EXPLAIN QUERY PLAN " + self.conn.query,
                                           self.conn.params).fetchall()
                details = "\n".join(row["detail"] for row in plan)
                # SQLite secondary indexes already carry the INTEGER PK as
                # their final rowid key, so it can choose the old two-column
                # covering index as well. PostgreSQL needs the explicit id.
                self.assertRegex(details, r"SEARCH ps USING (?:COVERING )?INDEX idx_snapshots_(?:latest|offer)")
                self.assertNotIn("SCAN ps", details)
                self.assertNotIn("SCAN price_snapshots", details)
                self.assertNotIn("PARTITION BY offer_id", self.conn.query)


if __name__ == "__main__":
    unittest.main()
