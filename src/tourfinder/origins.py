"""Supported departure airports, without rewriting stored offer identities."""
import re

BALTIC_ORIGINS = ("RIX", "VNO", "TLL")
JOINUP_ORIGINS = {"RIX": "3164", "VNO": "2151", "TLL": "2552"}


def normalize_origins(value: str | None = None) -> str:
    """Canonical public CSV; legacy searches without this filter mean Riga."""
    if value is None or value == "":
        return "RIX"
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("Unsupported departure airport")
    codes = {part.strip().upper() for part in value.split(",") if part.strip()}
    if not codes:
        return "RIX"
    if not codes <= set(BALTIC_ORIGINS):
        raise ValueError("Unsupported departure airport")
    return ",".join(sorted(codes))


def source_origin(source: str, code: str) -> str:
    """Translate one explicit requested airport to a provider parameter."""
    if not isinstance(code, str) or code.strip().upper() not in BALTIC_ORIGINS:
        raise ValueError("Unsupported departure airport")
    code = code.strip().upper()
    if source == "joinup":
        return JOINUP_ORIGINS[code]
    if source == "waavo":
        return code
    raise ValueError("Unsupported source")


def normalize_source_origin(source: str, value) -> str | None:
    """Interpret an actual provider echo; missing/unknown values stay unknown."""
    if type(value) not in (str, int):
        return None
    value = str(value).strip().upper()
    if value in BALTIC_ORIGINS:
        return value
    if source == "joinup":
        return next((code for code, identifier in JOINUP_ORIGINS.items() if identifier == value), None)
    return None


def origin_code_sql(alias: str = "o") -> str:
    """Portable expression; only server-owned identifiers/constants enter SQL."""
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("Invalid SQL alias")
    identifier = f"upper(trim({alias}.origin_id))"
    cases = [f"WHEN {identifier} IN ('RIX','VNO','TLL') THEN {identifier}"]
    cases.extend(f"WHEN {alias}.source='joinup' AND {identifier}='{value}' THEN '{code}'"
                 for code, value in JOINUP_ORIGINS.items())
    return "CASE " + " ".join(cases) + " END"


def filter_sql(value: str | None, params: dict, *, alias: str = "o") -> str:
    codes = normalize_origins(value).split(",")
    names = [f"origin{i}" for i in range(len(codes))]
    params.update(zip(names, codes))
    return f"({origin_code_sql(alias)}) IN ({','.join(':' + name for name in names)})"
