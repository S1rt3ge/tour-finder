"""Discovery meal checks must match real SQLite search filters, without a DB per row."""
import sqlite3

import pytest

from tourfinder import meals


@pytest.mark.parametrize("code,name,expected", [
    ("RO", "All inclusive", "RO"),  # Recognized source code wins over its name.
    ("SOFTAI", "Ultra all inclusive", "AI"),
    ("BB", "Full board", "BB"),
    ("HB+", None, "HB"),
    ("OT", "SELF_CATERING", "RO"),
    ("", "Super UAI", "UAI"),
    (None, "All inclusive (soft drinks only)", "AI"),
    ("opaque", "Ultra all inclusive premium", "UAI"),
    (None, "Viss iekļauts plus", "AI"),
    (" bb ", "Room only", "BB"),
    ("\tBB\t", None, "OTHER"),
    (None, "\tBreakfast\t", "OTHER"),
    (None, "_BREAKFAST_", "BB"),
    (None, "\u00a0Breakfast\u00a0", "OTHER"),
    (None, "Завтраки", "BB"),
    (None, "ЗАВТРАКИ", "OTHER"),
    (None, "Всё включено", "AI"),
    (None, "ВСЁ ВКЛЮЧЕНО", "OTHER"),
    (None, "VISS IEKĻAUTS", "OTHER"),
    ("АI", None, "OTHER"),  # Cyrillic A is not ASCII A.
    (None, "breakfast deluxe", "OTHER"),
    (None, "not all inclusive", "OTHER"),
    ("other", "unknown plan", "OTHER"),
    (None, None, "OTHER"),
])
def test_observed_meal_semantics_match_sql(code, name, expected):
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE offers(board_code TEXT,board_name TEXT)")
        conn.execute("INSERT INTO offers VALUES (?,?)", (code, name))
        actual_sql = conn.execute(f"SELECT {meals.category_sql()} FROM offers o").fetchone()[0]
    assert meals.category(code, name) == actual_sql == expected


def test_all_current_aliases_and_prefixes_match_sql_over_case_and_whitespace_variants():
    rows = set()
    for codes in meals._CODES.values():
        for code in codes:
            for variant in (code, code.lower(), code.upper(), " " + code + " ", "\t" + code):
                rows.add((variant, "Full board"))
    for names in meals._NAMES.values():
        for name in names:
            for variant in (name, name.lower(), name.upper(), name.title(), " " + name + " ",
                            name.replace(" ", "_"), "\t" + name, name + " extra"):
                rows.add(("unknown", variant))
    for _label, prefixes in meals._PREFIXES:
        for prefix in prefixes:
            for variant in (prefix, prefix.upper(), " " + prefix + " extra", prefix.replace(" ", "_")):
                rows.add((None, variant))
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE offers(board_code TEXT,board_name TEXT)")
        conn.executemany("INSERT INTO offers VALUES (?,?)", rows)
        actual = conn.execute(f"SELECT board_code,board_name,{meals.category_sql()} FROM offers o").fetchall()
    assert len(actual) > 300
    for code, name, expected in actual:
        assert meals.category(code, name) == expected, (code, name, expected)
