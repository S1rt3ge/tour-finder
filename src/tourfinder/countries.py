"""Country choices shared across providers, preserving legacy raw-ID filters.

Names are matched against explicit aliases, never by substring. Numeric IDs
are meaningful only within their evidenced source, and are used as a fallback
when the source omitted the name. Unknown IDs and names remain usable verbatim.
"""
import re

COUNTRY_NAMES = {"AL": "Албания", "BG": "Болгария", "CY": "Кипр", "EG": "Египет",
                 "GR": "Греция", "ME": "Черногория", "TN": "Тунис", "TR": "Турция"}
_NAMES = {
    "AL": ("Albania", "Албания", "Albānija", "Albanija", "Albaania"),
    "BG": ("Bulgaria", "Болгария", "Bulgārija", "Bulgarija"),
    "CY": ("Cyprus", "Кипр", "Kipra", "Kipras", "Küpros"),
    "EG": ("Egypt", "Египет", "Ēģipte", "Egipte", "Egiptas", "Egiptus"),
    "GR": ("Greece", "Греция", "Grieķija", "Griekija", "Graikija", "Kreeka"),
    "ME": ("Montenegro", "Черногория", "Melnkalne", "Juodkalnija"),
    "TN": ("Tunisia", "Тунис", "Tunisija", "Tuniisia"),
    "TR": ("Turkey", "Türkiye", "TÜRKİYE", "Turkiye", "Турция", "Turcija", "Turkija", "Türgi"),
}
# Join Up: docs/joinup-api-recon.md. Waavo IDs: inspected production country
# rows (including both 15 and 18 for Turkey); never apply these globally.
SOURCE_COUNTRY_IDS = {
    "joinup": {"c_122": "AL", "c_3": "BG", "c_50": "CY", "c_9": "EG",
               "c_4": "GR", "c_28": "ME", "c_58": "TN", "c_8": "TR"},
    "waavo": {"17": "BG", "28": "GR", "88": "ME", "52": "TN", "15": "TR", "18": "TR"},
}

# SQLite lower() is ASCII-only; explicit localized case variants keep the
# Python and SQL mappings portable instead of assuming Unicode SQL folding.
_ASCII_NAMES = {code: tuple(sorted({name.lower() for name in names if name.isascii()}))
                for code, names in _NAMES.items()}
_LOCAL_NAMES = {code: tuple(sorted({variant for name in names if not name.isascii()
                                  for variant in (name, name.lower(), name.upper(), name.title())}))
                for code, names in _NAMES.items()}


def _name_code(name):
    value = str(name or "").strip(" ")
    for code in COUNTRY_NAMES:
        if value.isascii() and value.lower() in _ASCII_NAMES[code] or value in _LOCAL_NAMES[code]:
            return code
    return None


def canonical_country_id(source, raw_id, name) -> str | None:
    code = _name_code(name)
    if code is None and not str(name or "").strip(" "):
        code = SOURCE_COUNTRY_IDS.get(source, {}).get(str(raw_id))
    if code:
        return "country:" + code
    return str(raw_id) if raw_id is not None else None


def canonical_options(rows) -> list[dict]:
    """Accept source/country_id/country_name rows; return stable public choices."""
    choices = {}
    for row in rows:
        row = dict(row)
        identifier = canonical_country_id(row.get("source"), row.get("country_id"), row.get("country_name"))
        if not identifier:
            continue
        code = identifier.removeprefix("country:") if identifier.startswith("country:") else None
        label = COUNTRY_NAMES.get(code) or str(row.get("country_name") or identifier)
        # Unknown repeated raw IDs retain their old filter meaning, while the
        # label choice is deterministic regardless of live/archive row order.
        choices[identifier] = min(choices.get(identifier, label), label)
    return [{"country_id": identifier, "country_name": choices[identifier]}
            for identifier in sorted(choices, key=lambda key: (choices[key], key))]


def normalize_countries(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("Invalid country filter")
    values = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("country:") and part[8:].upper() in COUNTRY_NAMES:
            part = "country:" + part[8:].upper()
        values.add(part)
    return ",".join(sorted(values)) or None


def _quote(value):
    return "'" + value.replace("'", "''") + "'"


def canonical_country_sql(alias: str = "h") -> str:
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("Invalid SQL alias")
    name = f"trim(coalesce({alias}.country_name,''))"
    cases = []
    for code in COUNTRY_NAMES:
        conditions = [f"lower({name}) IN ({','.join(map(_quote, _ASCII_NAMES[code]))})"]
        if _LOCAL_NAMES[code]:
            conditions.append(f"{name} IN ({','.join(map(_quote, _LOCAL_NAMES[code]))})")
        cases.append(f"WHEN {' OR '.join(conditions)} THEN 'country:{code}'")
    for source, identifiers in SOURCE_COUNTRY_IDS.items():
        for identifier, code in identifiers.items():
            cases.append(f"WHEN {name}='' AND {alias}.source={_quote(source)} "
                         f"AND {alias}.country_id={_quote(identifier)} THEN 'country:{code}'")
    return "CASE " + " ".join(cases) + f" ELSE {alias}.country_id END"


def filter_sql(value: str | None, params: dict, *, alias: str = "h") -> str | None:
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("Invalid SQL alias")
    normalized = normalize_countries(value)
    if not normalized:
        return None
    values = normalized.split(",")
    clauses = []
    for canonical in (False, True):
        selected = [item for item in values
                    if (item.startswith("country:") and item[8:] in COUNTRY_NAMES) is canonical]
        if not selected:
            continue
        names = [f"country_{int(canonical)}_{index}" for index in range(len(selected))]
        params.update(zip(names, selected))
        # Legacy IDs match exactly as before; they do not expand into aliases.
        field = canonical_country_sql(alias) if canonical else f"{alias}.country_id"
        clauses.append(f"({field}) IN ({','.join(':' + name for name in names)})")
    return "(" + " OR ".join(clauses) + ")"
