"""Canonical country choices/filtering on isolated in-memory SQLite."""
import sqlite3

import pytest

from tourfinder import countries


@pytest.mark.parametrize("source,identifier,name,expected", [
    ("joinup", "c_8", "Turcija", "country:TR"),
    ("waavo", "15", "Turkey", "country:TR"),
    ("waavo", "18", "Türkiye", "country:TR"),
    ("waavo", "18", "TÜRKİYE", "country:TR"),
    ("joinup", "c_3", "Болгария", "country:BG"),
    ("waavo", "17", "BULGARIA", "country:BG"),
    ("joinup", "c_4", "Grieķija", "country:GR"),
    ("waavo", "28", "Greece", "country:GR"),
    ("joinup", "c_28", "Черногория", "country:ME"),
    ("waavo", "88", "Montenegro", "country:ME"),
    ("joinup", "c_58", "ТУНИС", "country:TN"),
    ("waavo", "52", "Tunisia", "country:TN"),
    ("joinup", "c_9", "", "country:EG"),
    ("joinup", "c_50", None, "country:CY"),
    ("waavo", "18", None, "country:TR"),
    ("other", "18", None, "18"),
    ("waavo", "c_8", None, "c_8"),
    ("joinup", "18", None, "18"),
    ("waavo", "15", "Unknown place", "15"),
    ("joinup", "c_8", "Greece", "country:GR"),
    ("unknown", "raw", "Turkey Bay", "raw"),
    ("unknown", "country:ZZ", "Unknown", "country:ZZ"),
    ("unknown", None, None, None),
])
def test_canonical_names_and_evidenced_source_ids_match_portable_sql(source, identifier, name, expected):
    assert countries.canonical_country_id(source, identifier, name) == expected
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE hotels(source TEXT,country_id TEXT,country_name TEXT)")
        conn.execute("INSERT INTO hotels VALUES (?,?,?)", (source, identifier, name))
        assert conn.execute(f"SELECT {countries.canonical_country_sql()} FROM hotels h").fetchone()[0] == expected


def test_options_merge_provider_ids_preserve_unknown_choices_and_are_order_independent():
    rows = [{"source": source, "country_id": raw, "country_name": name} for source, raw, name in [
        ("joinup", "c_8", "Турция"), ("waavo", "15", "Turkey"), ("waavo", "18", "Turcija"),
        ("joinup", "c_3", "Болгария"), ("waavo", "17", "Bulgaria"),
        ("joinup", "c_4", "Греция"), ("waavo", "28", "Greece"),
        ("joinup", "c_28", "Черногория"), ("waavo", "88", "Montenegro"),
        ("joinup", "c_58", "Тунис"), ("waavo", "52", "Tunisia"),
        ("other", "custom", "Unknown country"), ("other", "custom", "Different raw label"),
    ]]
    result = countries.canonical_options(rows)
    assert result == countries.canonical_options(reversed(rows))
    assert {row["country_id"] for row in result} == {"country:TR", "country:BG", "country:GR", "country:ME", "country:TN", "custom"}
    assert next(row for row in result if row["country_id"] == "country:TR")["country_name"] == "Турция"
    assert countries.canonical_options(result) == result


def test_country_filter_normalization_preserves_raw_meaning():
    assert countries.normalize_countries(None) is None
    assert countries.normalize_countries(" , ") is None
    assert countries.normalize_countries(" c_8, country:tr, 15,c_8 ") == "15,c_8,country:TR"
    assert countries.normalize_countries("country:ZZ, Weird_ID") == "Weird_ID,country:ZZ"


def test_raw_and_canonical_filters_have_distinct_safe_semantics():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE hotels(source TEXT,country_id TEXT,country_name TEXT)")
        rows = [("joinup", "c_8", "Turkey"), ("waavo", "15", "Turkey"),
                ("waavo", "18", "Turkey"), ("other", "15", "Unknown"),
                ("other", "country:ZZ", "Unknown"), ("other", "c_8' OR 1=1 --", "Unknown")]
        conn.executemany("INSERT INTO hotels VALUES (?,?,?)", rows)

        def selected(value):
            params = {}
            clause = countries.filter_sql(value, params)
            return conn.execute(f"SELECT source,country_id,country_name FROM hotels h WHERE {clause}", params).fetchall()

        assert selected("country:TR") == rows[:3]
        assert selected("15") == [rows[1], rows[3]]
        assert selected("c_8") == [rows[0]]
        assert selected("country:TR,country:ZZ") == rows[:3] + [rows[4]]
        assert selected("c_8' OR 1=1 --") == [rows[5]]


@pytest.mark.parametrize("value", ["c_8", "country:TR"])
def test_country_sql_alias_cannot_be_injected(value):
    with pytest.raises(ValueError):
        countries.filter_sql(value, {}, alias="h; DROP TABLE hotels; --")
