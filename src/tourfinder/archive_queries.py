"""Public browsing across live storage and verified, read-only archives.

This layer is deliberately absent from notification evaluation. Archived
prices are always labelled stale. Database connections are supplied by the
caller; importing this module never opens the application's database.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from . import meals, queries, reviews
from .archive_format import ArchiveError, normalize_offer_key, observation_key, offer_key
from .archive_store import ArchiveStore

CANDIDATE_LIMIT = 2000
HISTORY_DISPLAY_LIMIT = 2000
HISTORY_READ_LIMIT = 10000
_IDENTITY = ("source", "source_hotel_id", "origin_id", "date_start", "nights",
             "board_code", "room_code", "room_placement", "pax_adl", "pax_chd", "children_ages")
_FILTER_DEFAULTS = dict(adults=2, children_ages=None, nights_min=1, nights_max=30,
                        budget_max=None, boards=None, board_categories=None,
                        countries=None, only_hot=False, stars_min=None,
                        hotel_id=None, source=None)
_STATS = ("snapshots_count", "avg_seen_cents", "min_seen_cents", "max_seen_cents")


class ReadUnavailable(RuntimeError):
    """Safe for a 503 response: never includes a connection string or URL."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def parse_offer_id(value):
    if isinstance(value, str) and value.startswith("a_"):
        return "a_" + normalize_offer_key(value)
    if type(value) is int and 0 < value <= 9223372036854775807:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        number = int(value)
        if 0 < number <= 9223372036854775807:
            return number
    raise ValueError("offer_id_invalid")


def _status():
    return dict(live="unavailable", archive="not_configured", mode="unavailable",
                archive_as_of=None, partial=False, partial_reasons=[])


def _note_partial(status, reason):
    status["partial"] = True
    if reason not in status["partial_reasons"]:
        status["partial_reasons"].append(reason)


def _finish(status, rows):
    sources = {row.get("data_source") for row in rows}
    status["mode"] = ("mixed" if len(sources) > 1 else next(iter(sources)) if sources
                      else "live" if status["live"] in {"ok", "empty"}
                      else "archive" if status["archive"] == "ok" else "unavailable")
    status["partial"] = status["partial"] or status["live"] == "unavailable" or status["archive"] == "unavailable"
    if status["mode"] == "unavailable":
        raise ReadUnavailable("read_sources_unavailable")
    return status


def _label(row, *, archived=False, as_of=None, dataset_id=None):
    row = dict(row)
    row.update(data_source="archive" if archived else "live", archived=archived,
               archive_as_of=as_of, dataset_id=dataset_id)
    cutoff = queries._fresh_cutoff()
    row["stale"] = bool(archived or not row.get("last_seen_at") or row["last_seen_at"] < cutoff
                        or not row.get("fetched_at") or row["fetched_at"] < cutoff
                        or row["date_start"] < datetime.now(timezone.utc).date().isoformat())
    row["star_gap"] = reviews.star_gap(row.get("category"), row.get("review_rating"), row.get("review_scale"))
    return meals.add_labels([row])[0]


def _sort_key(row, sort):
    price = row["price_cents"] / row["nights"] if sort == "price_per_night" else row["price_cents"]
    return price, row["source"], row["source_hotel_id"], str(row["offer_id"])


def _group(rows, sort):
    result = {}
    for row in sorted(rows, key=lambda r: _sort_key(r, sort)):
        key = row["source"], row["source_hotel_id"]
        count = row.get("variants", 1)
        low, high = row.get("variants_min_cents", row["price_cents"]), row.get("variants_max_cents", row["price_cents"])
        start, end = row.get("variants_date_from", row["date_start"]), row.get("variants_date_till", row["date_start"])
        if key not in result:
            result[key] = dict(row, variants=count, variants_min_cents=low, variants_max_cents=high,
                               variants_date_from=start, variants_date_till=end,
                               archived_variants=count if row.get("archived") else 0)
        else:
            found = result[key]
            found["variants"] += count
            found["variants_min_cents"] = min(found["variants_min_cents"], low)
            found["variants_max_cents"] = max(found["variants_max_cents"], high)
            found["variants_date_from"] = min(found["variants_date_from"], start)
            found["variants_date_till"] = max(found["variants_date_till"], end)
            found["archived_variants"] += count if row.get("archived") else 0
    return list(result.values())


def _merge_compositions(live, cold):
    combined = {}
    for name, rows in (("live", live), ("archive", cold)):
        for row in rows:
            key = row["pax_adl"], row["pax_chd"], row["children_ages"]
            entry = combined.setdefault(key, dict(row, live_offers=0, archived_offers=0))
            entry["live_offers" if name == "live" else "archived_offers"] = row["offers"]
            # Counts overlap across stores; do not fabricate a union count.
            entry["offers"] = None if entry["live_offers"] and entry["archived_offers"] else row["offers"]
    return [combined[key] for key in sorted(combined)]


def _live_matches(conn, candidates):
    """Indexed natural-key probes without ANY price/freshness/search filters."""
    found = {}
    unique = {row["offer_key"]: row for row in candidates}
    values = list(unique.values())
    for start in range(0, len(values), 40):
        batch, params, clauses = values[start:start + 40], {}, []
        for i, row in enumerate(batch):
            conditions = []
            for field in _IDENTITY:
                name = f"v{i}_{field}"
                params[name] = queries._norm_ages(row[field]) if field == "children_ages" else row[field]
                conditions.append(f"{field}=:{name}")
            clauses.append("(" + " AND ".join(conditions) + ")")
        for raw in conn.execute("SELECT * FROM offers WHERE " + " OR ".join(clauses), params):
            row = dict(raw)
            found[offer_key(row)] = row
    return found


def _add_live_keys(conn, rows):
    if not rows:
        return rows
    params = {f"i{i}": row["offer_id"] for i, row in enumerate(rows)}
    ids = ",".join(":" + name for name in params)
    identity = {row["id"]: offer_key(dict(row)) for row in conn.execute(f"SELECT * FROM offers WHERE id IN ({ids})", params)}
    return [dict(row, offer_key=identity[row["offer_id"]]) for row in rows]


def _cold_candidates(conn, filters, sort):
    where, snapshot_where, params = queries._build_filters(**filters, limit=100)
    where.remove("o.last_seen_at >= :fresh_cutoff")
    params["candidate_limit"] = CANDIDATE_LIMIT + 1
    sql = f"""WITH {queries._candidates_sql(where)}, selected AS MATERIALIZED (
        {queries._matched_sql(snapshot_where, queries.SORTS.get(sort, queries.SORTS['price']))}
        ORDER BY sort_key,offer_id LIMIT :candidate_limit)
        SELECT m.*, {queries._REVIEW_COLUMNS}, k.offer_key,o.origin_id,o.room_code
        FROM selected m {queries._REVIEW_JOIN}
        JOIN archive_offer_keys k ON k.offer_id=m.offer_id JOIN offers o ON o.id=m.offer_id
        ORDER BY m.sort_key,m.offer_id"""
    rows = [dict(row) for row in conn.execute(sql, params)]
    clipped = len(rows) > CANDIDATE_LIMIT
    for row in rows:
        row.pop("sort_key", None)
        row.update({field: None for field in _STATS})
        row["catalog_offer_id"] = row["offer_id"]
        row["offer_id"] = "a_" + row["offer_key"]
    return rows[:CANDIDATE_LIMIT], clipped


class ReadService:
    def __init__(self, *, connect, archive_store=None):
        self._connect = connect
        self._archive_error = None
        try:
            self._archive = archive_store if archive_store is not None else ArchiveStore()
        except ArchiveError:
            self._archive, self._archive_error = None, "archive_configuration_invalid"

    @contextmanager
    def _live(self, status):
        conn = None
        try:
            conn = self._connect()
        except Exception:
            pass
        try:
            yield conn
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _configured(self, status):
        if self._archive_error:
            status.update(archive="unavailable", archive_error=self._archive_error)
            return False
        return self._archive is not None and self._archive.configured()

    def _archive_failure(self, status):
        status.update(archive="unavailable", archive_error="archive_read_failed", partial=True)

    def search(self, *, group=True, sort="price", limit=100, **filters):
        filters = _FILTER_DEFAULTS | filters
        limit = max(1, min(int(limit), 500))
        status, live_rows, cold_rows = _status(), [], []
        cold_compositions = None
        archive_configured = self._configured(status)
        with self._live(status) as conn:
            if conn is not None:
                try:
                    function = queries.search_hotels_grouped if group else queries.search_offers
                    live_rows = _add_live_keys(conn, function(conn, sort=sort, limit=limit, **filters))
                    status["live"] = "ok" if live_rows else "empty"
                except Exception:
                    live_rows = []
            # Full live pages need no network or cold catalog scan. Otherwise
            # date-filtered cold candidates can fill missing live coverage.
            if archive_configured:
                if len(live_rows) >= limit:
                    status["archive"] = "not_needed"
                else:
                    try:
                        with self._archive.load_catalog() as catalog:
                            candidates, clipped = _cold_candidates(catalog, filters, sort)
                            status.update(archive="ok", archive_as_of=catalog.manifest["catalog"]["created_at"])
                            if status["live"] in {"ok", "empty"}:
                                # If this check fails, discard cold results:
                                # never resurrect old cheap prices by accident.
                                try:
                                    blocked = _live_matches(conn, candidates)
                                except Exception:
                                    status.update(partial=True, archive="unavailable", archive_error="live_identity_check_failed")
                                    candidates = []
                                    blocked = {}
                            else:
                                blocked = {}
                            cold_rows = [_label(row, archived=True, as_of=status["archive_as_of"],
                                               dataset_id=catalog.manifest["dataset_id"])
                                         for row in candidates if row["offer_key"] not in blocked]
                            if clipped:
                                _note_partial(status, "archive_candidate_limit")
                            if not live_rows and not cold_rows:
                                try:
                                    cold_compositions = queries.available_compositions(catalog)
                                except Exception:
                                    _note_partial(status, "composition_lookup_failed")
                    except Exception:
                        self._archive_failure(status)
            live_rows = [_label(row) for row in live_rows]
            if archive_configured:
                for row in live_rows:
                    row.update({field: None for field in _STATS})
                    row["history_scope"] = "load_history"
            rows = live_rows + cold_rows
            if group:
                rows = _group(rows, sort)
            rows = sorted(rows, key=lambda row: _sort_key(row, sort))[:limit]
            compositions = None
            if not rows:
                live_compositions = None
                if status["live"] in {"ok", "empty"}:
                    try:
                        live_compositions = queries.available_compositions(conn)
                    except Exception:
                        _note_partial(status, "composition_lookup_failed")
                if live_compositions is not None or cold_compositions is not None:
                    compositions = _merge_compositions(live_compositions or [], cold_compositions or [])
        return dict(count=len(rows), results=rows, available_compositions=compositions,
                    queued_spec=None, storage=_finish(status, rows))

    def _resolve(self, conn, identifier, status):
        """Return (public detail, raw live identity, stable key)."""
        archived = isinstance(identifier, str)
        cold, raw = None, None
        if archived and self._configured(status):
            try:
                cold = self._archive.lookup_offer(identifier)
                status.update(archive="ok", archive_as_of=cold.get("archive_as_of") if cold else None)
            except Exception:
                self._archive_failure(status)
        if conn is not None:
            try:
                if archived:
                    matches = _live_matches(conn, [cold]) if cold else {}
                    raw = matches.get(normalize_offer_key(identifier))
                else:
                    record = conn.execute("SELECT * FROM offers WHERE id=:id", {"id": identifier}).fetchone()
                    raw = dict(record) if record else None
                status["live"] = "ok" if raw else "empty"
                if raw:
                    detail = queries.offer_detail(conn, raw["id"])
                    key = offer_key(raw)
                    if detail:
                        return _label(dict(detail, offer_key=key)), raw, key
                    # Known live identity with no snapshot still suppresses a
                    # stale archive price, rather than pretending it is live.
                    return None, raw, key
            except Exception:
                status["live"] = "unavailable"
        if cold:
            cold["board_category"] = cold.get("board_category") or self._cold_meal(cold)
            cold["price_per_night_cents"] = cold["price_cents"] // cold["nights"]
            return _label(cold, archived=True, as_of=cold["archive_as_of"], dataset_id=cold["dataset_id"]), None, cold["offer_key"]
        return None, raw, normalize_offer_key(identifier) if archived else None

    @staticmethod
    def _cold_meal(row):
        # Reuse the authoritative portable CASE without duplicating mappings.
        import sqlite3
        with sqlite3.connect(":memory:") as conn:
            return conn.execute(f"SELECT {meals.category_sql()} FROM (SELECT :code AS board_code,:name AS board_name) o",
                                {"code": row.get("board_code"), "name": row.get("board_name")}).fetchone()[0]

    def detail(self, identifier):
        identifier = parse_offer_id(identifier)
        status = _status()
        with self._live(status) as conn:
            row, _raw, _key = self._resolve(conn, identifier, status)
        if isinstance(identifier, str) and status["archive"] == "unavailable" and row is None:
            raise ReadUnavailable("archive_identity_unavailable")
        if row and self._configured(status):
            # Hot retention statistics are incomplete; the history endpoint
            # computes complete statistics after merging the targeted shard.
            row.update({field: None for field in _STATS})
            row["history_scope"] = "load_history"
        return {"offer": row, "storage": _finish(status, [row] if row else [])}

    def history(self, identifier):
        identifier = parse_offer_id(identifier)
        status, live_history, cold_history = _status(), [], []
        with self._live(status) as conn:
            detail, raw, key = self._resolve(conn, identifier, status)
            if raw:
                try:
                    fetched = conn.execute("""SELECT * FROM price_snapshots WHERE offer_id=:id
                        ORDER BY fetched_at DESC,id DESC LIMIT :limit""",
                        {"id": raw["id"], "limit": HISTORY_READ_LIMIT + 1}).fetchall()
                    if len(fetched) > HISTORY_READ_LIMIT:
                        raise ReadUnavailable("history_read_limit_exceeded")
                    live_history = [dict(row, observation_key=observation_key(key, dict(row)), data_source="live") for row in fetched]
                except Exception:
                    status.update(live="unavailable", partial=True)
            if key and self._configured(status):
                try:
                    cold_history = self._archive.history(key)
                    status["archive"] = "ok"
                    if not status["archive_as_of"]:
                        status["archive_as_of"] = self._archive.manifest()["created_at"]
                except Exception:
                    self._archive_failure(status)
        by_fingerprint = {row["observation_key"]: row for row in cold_history}
        by_fingerprint.update({row["observation_key"]: row for row in live_history})
        # Snapshot IDs may restart after a restore. At the same timestamp the
        # current live observation wins over archived IDs from an older DB.
        rows = sorted(by_fingerprint.values(), key=lambda row: (
            row["fetched_at"], row["data_source"] == "live",
            row.get("id", row.get("snapshot_id", 0)), row["observation_key"]))
        if detail is None and isinstance(identifier, str) and status["archive"] == "unavailable":
            raise ReadUnavailable("archive_identity_unavailable")
        _finish(status, rows)
        currency = detail.get("currency") if detail else None
        prices = [row["price_cents"] for row in rows if currency is None or row["currency"] == currency]
        complete = status["live"] != "unavailable" and status["archive"] != "unavailable"
        stats = dict(snapshots_count=len(rows), avg_seen_cents=sum(prices) / len(prices) if prices else None,
                     min_seen_cents=min(prices) if prices else None, max_seen_cents=max(prices) if prices else None,
                     currency=currency, complete=complete)
        return dict(offer_id=identifier, gone=bool(detail and detail["stale"]),
                    last_seen_at=detail.get("last_seen_at") if detail else None,
                    history=rows[-HISTORY_DISPLAY_LIMIT:], history_total=len(rows),
                    history_truncated=len(rows) > HISTORY_DISPLAY_LIMIT, statistics=stats, storage=status)

    def compositions(self):
        status, live, cold = _status(), [], []
        with self._live(status) as conn:
            if conn is not None:
                try:
                    live = queries.available_compositions(conn)
                    status["live"] = "ok" if live else "empty"
                except Exception:
                    pass
        if self._configured(status):
            try:
                with self._archive.load_catalog() as catalog:
                    cold = queries.available_compositions(catalog)
                    status.update(archive="ok", archive_as_of=catalog.manifest["catalog"]["created_at"])
            except Exception:
                self._archive_failure(status)
        origins = ([{"data_source": "live"}] if live else []) + ([{"data_source": "archive"}] if cold else [])
        return dict(compositions=_merge_compositions(live, cold), storage=_finish(status, origins))

    def options(self):
        """Small filter dictionaries; callers expose them only after auth."""
        status = _status()
        live, cold = {"countries": [], "boards": []}, {"countries": [], "boards": []}

        def read(conn):
            return {
                "countries": [dict(row) for row in conn.execute("""SELECT DISTINCT country_id,country_name
                    FROM hotels WHERE country_id IS NOT NULL ORDER BY country_name""")],
                "boards": [dict(row) for row in conn.execute("""SELECT board_code,max(board_name) AS board_name
                    FROM offers GROUP BY board_code ORDER BY board_code""")],
            }

        with self._live(status) as conn:
            if conn is not None:
                try:
                    live = read(conn)
                    status["live"] = "ok" if any(live.values()) else "empty"
                except Exception:
                    pass
        if self._configured(status):
            if live["countries"] and live["boards"]:
                status["archive"] = "not_needed"
            else:
                try:
                    with self._archive.load_catalog() as catalog:
                        cold = read(catalog)
                        status.update(archive="ok", archive_as_of=catalog.manifest["catalog"]["created_at"])
                except Exception:
                    self._archive_failure(status)
        result = {}
        for name, identity, label in (("countries", "country_id", "country_name"), ("boards", "board_code", "board_name")):
            combined = {row[identity]: row for row in cold[name]}
            combined.update({row[identity]: row for row in live[name]})
            result[name] = sorted(combined.values(), key=lambda row: (str(row.get(label) or ""), str(row[identity])))
        origins = ([{"data_source": "live"}] if any(live.values()) else []) + ([{"data_source": "archive"}] if any(cold.values()) else [])
        return result | {"storage": _finish(status, origins)}
