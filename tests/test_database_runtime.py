"""Cold-start behavior must not mutate or block on the production schema."""
from tourfinder import db


def test_serverless_postgres_defers_schema_work_to_migrations(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture:fixture@invalid.example/fixture")
    monkeypatch.setenv("VERCEL", "1")
    calls = []
    engine = object()
    monkeypatch.setattr(db, "_engines", {})
    monkeypatch.setattr(db, "create_engine", lambda url, **kwargs: calls.append(kwargs) or engine)
    monkeypatch.setattr(db.metadata, "create_all", lambda *_: (_ for _ in ()).throw(AssertionError("web DDL")))
    monkeypatch.setattr(db, "_ensure_new_columns", lambda *_: (_ for _ in ()).throw(AssertionError("web ALTER")))
    assert db.get_engine() is engine
    assert db.get_engine() is engine
    assert len(calls) == 1
    assert calls[0]["connect_args"]["connect_timeout"] == 8
