"""Departure normalization tests never contact providers or a configured DB."""
import sqlite3

import pytest

from tourfinder import origins


@pytest.mark.parametrize("value,expected", [
    (None, "RIX"), ("", "RIX"), (" , ", "RIX"), ("rix", "RIX"),
    (" vno, RIX,tll, VNO ", "RIX,TLL,VNO"), ("TLL,VNO", "TLL,VNO"),
])
def test_public_csv_has_stable_legacy_default(value, expected):
    assert origins.normalize_origins(value) == expected


@pytest.mark.parametrize("value", ["LON", "RIX,LON", "3164", "RIX;VNO", ["RIX"], 1, True, "RIX," * 40])
def test_public_csv_rejects_unsupported_or_malformed_airports(value):
    with pytest.raises(ValueError):
        origins.normalize_origins(value)


@pytest.mark.parametrize("code,identifier", [("RIX", "3164"), ("VNO", "2151"), ("TLL", "2552")])
def test_provider_parameter_mapping(code, identifier):
    assert origins.source_origin("joinup", code) == identifier
    assert origins.source_origin("waavo", code) == code
    assert origins.normalize_source_origin("joinup", identifier) == code
    assert origins.normalize_source_origin("joinup", code) == code
    assert origins.normalize_source_origin("waavo", identifier) is None
    assert origins.normalize_source_origin("waavo", code.lower()) == code


@pytest.mark.parametrize("source,value", [("joinup", None), ("waavo", ""), ("joinup", "9999"),
                                          ("waavo", "LON"), ("joinup", False), ("waavo", ["VNO"])])
def test_missing_or_unknown_echo_never_becomes_riga(source, value):
    assert origins.normalize_source_origin(source, value) is None


@pytest.mark.parametrize("source,value", [("unknown", "RIX"), ("joinup", None), ("joinup", ""),
                                          ("waavo", "RIX,VNO"), ("waavo", "3164")])
def test_provider_request_requires_one_explicit_supported_airport(source, value):
    with pytest.raises(ValueError):
        origins.source_origin(source, value)


def test_sql_matches_python_without_changing_raw_identity():
    rows = [("joinup", "3164"), ("joinup", "2151"), ("joinup", "2552"),
            ("joinup", "RIX"), ("waavo", "VNO"), ("waavo", "tll"),
            ("waavo", "3164"), ("other", "2151"), ("joinup", "LON"), ("waavo", None)]
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE offers(source TEXT,origin_id TEXT)")
        conn.executemany("INSERT INTO offers VALUES (?,?)", rows)
        result = conn.execute(f"SELECT o.source,o.origin_id,{origins.origin_code_sql()} FROM offers o").fetchall()
        assert result == [(source, raw, origins.normalize_source_origin(source, raw)) for source, raw in rows]
        params = {}
        clause = origins.filter_sql("TLL,VNO", params)
        selected = conn.execute(f"SELECT o.source,o.origin_id FROM offers o WHERE {clause}", params).fetchall()
        assert selected == [("joinup", "2151"), ("joinup", "2552"), ("waavo", "VNO"), ("waavo", "tll")]


def test_dynamic_values_are_bound_and_sql_alias_is_validated():
    params = {}
    query = origins.filter_sql("VNO", params)
    assert params == {"origin0": "VNO"} and ":origin0" in query
    with pytest.raises(ValueError):
        origins.origin_code_sql("o); DROP TABLE offers; --")
