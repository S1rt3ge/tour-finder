"""Source-independent meal categories; original meal names remain available."""

LABELS = {
    "RO": "Без питания",
    "BB": "Завтраки",
    "HB": "Двухразовое питание",
    "FB": "Трёхразовое питание",
    "AI": "Всё включено",
    "UAI": "Ультра всё включено",
    "OTHER": "Другое / не указано",
}

# Codes are authoritative when the source supplies a recognized code. Some
# operators omit codes or use OT for self catering, so names are a fallback.
_CODES = {
    "RO": ("RO", "RR", "OB", "SC", "AO", "EP"),
    "BB": ("BB", "B&B", "CP"),
    "HB": ("HB", "HB+", "MAP"),
    "FB": ("FB", "FB+", "AP"),
    "AI": ("AI", "ALL", "ALL INCLUSIVE", "SOFTAI", "SOFT AI", "SAI"),
    "UAI": ("UAI", "ULTRA AI", "ULTRA ALL INCLUSIVE", "SUPER UAI"),
}
_NAMES = {
    "RO": ("room only", "self catering", "self-catering", "without meals", "no meals",
           "bez ēdināšanas", "tikai numurs", "без питания", "Без питания"),
    "BB": ("bed and breakfast", "bed & breakfast", "breakfast", "brokastis",
           "ar brokastīm", "nakšņošana un brokastis", "завтраки", "Завтраки"),
    "HB": ("half board", "halfboard", "puspansija", "brokastis un vakariņas",
           "полупансион", "Полупансион"),
    "FB": ("full board", "fullboard", "pilna pansija", "полный пансион", "Полный пансион"),
    "AI": ("all inclusive", "soft ai", "viss iekļauts", "все включено", "всё включено", "Все включено", "Всё включено"),
    "UAI": ("uai", "super uai", "ultra ai", "ultra all inclusive", "super all inclusive",
            "ultra viss iekļauts", "ультра всё включено", "Ультра всё включено"),
}
_PREFIXES = (("UAI", ("ultra all inclusive", "super uai")),
             ("AI", ("all inclusive", "viss iekļauts", "soft ai")))
_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def category(board_code: str | None, board_name: str | None) -> str:
    """Classify one observed meal using the same rules as SQLite category_sql.

    SQLite's built-in UPPER/LOWER fold ASCII only; TRIM removes U+0020, not
    arbitrary whitespace. Preserve those details rather than broadening a
    user's category filter when a discovery adapter checks a row in Python.
    """
    code = str(board_code if board_code is not None else "").strip(" ").translate(_ASCII_UPPER)
    name = str(board_name if board_name is not None else "").replace("_", " ").strip(" ").translate(_ASCII_LOWER)
    for label, codes in _CODES.items():
        if code in codes:
            return label
    for label, names in _NAMES.items():
        if name in names:
            return label
    for label, prefixes in _PREFIXES:
        if name.startswith(prefixes):
            return label
    return "OTHER"


def normalize_categories(value: str | None) -> str | None:
    if not value:
        return None
    categories = list(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))
    if any(part not in LABELS for part in categories):
        raise ValueError("Unknown meal category")
    return ",".join(categories) or None


def category_sql(alias: str = "o") -> str:
    """Portable CASE for SQLite/Postgres, using only server-owned literals."""
    if alias != "o":
        raise ValueError("Unsupported SQL alias")
    code = "UPPER(TRIM(COALESCE(o.board_code,'')))"
    name = "LOWER(TRIM(REPLACE(COALESCE(o.board_name,''),'_',' ')))"
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    cases = []
    for category, codes in _CODES.items():
        values = ",".join(quote(item) for item in codes)
        cases.append(f"WHEN {code} IN ({values}) THEN '{category}'")
    for category, names in _NAMES.items():
        values = ",".join(quote(item) for item in names)
        cases.append(f"WHEN {name} IN ({values}) THEN '{category}'")
    # Preserve operator-specific qualifiers in board_name; these only group
    # the meal plan. They do not promise identical drinks or service levels.
    for category, prefixes in _PREFIXES:
        matches = " OR ".join(f"{name} LIKE {quote(prefix + '%')}" for prefix in prefixes)
        cases.append(f"WHEN {matches} THEN '{category}'")
    return "CASE " + " ".join(cases) + " ELSE 'OTHER' END"


def add_labels(rows: list[dict]) -> list[dict]:
    for row in rows:
        row["board_label"] = LABELS.get(row.get("board_category"), LABELS["OTHER"])
    return rows
