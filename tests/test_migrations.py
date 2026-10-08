import sqlite3
from types import SimpleNamespace

import pytest

from tourfinder import db, migrate


def test_legacy_additive_migration_preserves_rows_and_delivery_identifiers(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE subscriptions(id INTEGER PRIMARY KEY,name TEXT NOT NULL,filters TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
        CREATE TABLE alerts(id INTEGER PRIMARY KEY,subscription_id INTEGER NOT NULL,offer_id INTEGER NOT NULL,reason TEXT NOT NULL,price_cents INTEGER NOT NULL,created_at TEXT NOT NULL,seen INTEGER NOT NULL DEFAULT 0);
        INSERT INTO subscriptions VALUES(5,'old','{}',1,'2026-01-01');
        INSERT INTO alerts VALUES(8,5,1,'new_match',10000,'2026-01-01',0);
    """)
    legacy.commit()
    legacy.close()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + path.as_posix())
    conn = db.connect()
    try:
        assert conn.execute("SELECT name,owner_id FROM subscriptions").fetchone() == {"name": "old", "owner_id": None}
        assert conn.execute("SELECT price_cents,evidence FROM alerts").fetchone() == {"price_cents": 10000, "evidence": "{}"}
        conn.execute("INSERT INTO telegram_deliveries(alert_id,status,owner_id,subscription_id) VALUES (8,'sent','123',5)")
        conn.execute("DELETE FROM alerts")
        conn.execute("DELETE FROM subscriptions")
        conn.commit()
        # The audit record must never collide with a newly allocated legacy ID.
        assert conn.next_retained_id("alerts") == 9
        assert conn.next_retained_id("subscriptions") == 6
        conn.commit()
        assert conn.next_retained_id("alerts") == 10
        assert conn.next_retained_id("subscriptions") == 7
        conn.commit()
        migrate.main()
        migrate.main()  # idempotent
        indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_snapshots_latest", "idx_subscriptions_owner", "idx_telegram_delivery_owner"} <= indexes
        assert conn.execute("SELECT count(*) FROM telegram_deliveries").scalar() == 1
    finally:
        conn.close()
        db._engines.pop("sqlite:///" + path.as_posix()).dispose()


class PostgreSQLMigrationConnection:
    """Catalog fixture: no PostgreSQL credentials or database are needed."""

    def __init__(self, existing=None, built=None):
        self.existing = existing
        self.built = built
        self.created = []
        self.autocommit = False

    def execution_options(self, **options):
        self.autocommit = options == {"isolation_level": "AUTOCOMMIT"}
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def exec_driver_sql(self, sql, params=None):
        if sql.lstrip().startswith("SELECT"):
            # Protect against treating a same-named index in another schema as
            # the index of the table being migrated.
            assert "c.relnamespace=target.relnamespace" in sql
            assert "target.oid=to_regclass(%s)" in sql
            name, table = params
            columns = {
                "price_snapshots": ["offer_id", "fetched_at", "id"],
                "subscriptions": ["owner_id", "enabled"],
                "telegram_deliveries": ["owner_id", "sent_at"],
                "telegram_access_requests": ["status", "requested_at"],
            }[table]
            if name in self.created:
                value = valid_index(columns) if self.built is None else self.built
            else:
                value = self.existing
            return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: value))
        assert self.autocommit
        assert sql.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS ")
        self.created.append(sql.split()[6])


def valid_index(columns=None, **changes):
    columns = columns or ["offer_id", "fetched_at", "id"]
    return {"valid": True, "ready": True, "correct_table": True, "plain": True,
            "method": "btree", "columns": columns, "options": [0] * len(columns), **changes}


def postgres_engine(monkeypatch, connection):
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: connection)
    monkeypatch.setattr(db, "get_engine", lambda: engine)


def test_postgresql_builds_indexes_in_autocommit_and_verifies_catalog(monkeypatch, capsys):
    connection = PostgreSQLMigrationConnection()
    postgres_engine(monkeypatch, connection)
    migrate.main()
    assert connection.created == ["idx_snapshots_latest", "idx_subscriptions_owner", "idx_telegram_delivery_owner", "idx_telegram_access_status"]
    assert "ready" in capsys.readouterr().out


@pytest.mark.parametrize("state", [
    valid_index(valid=False), valid_index(ready=False),
    valid_index(correct_table=False), valid_index(columns=["offer_id"]),
    valid_index(plain=False), valid_index(method="hash"),
    valid_index(options=[0, 0, 3]),
])
def test_postgresql_refuses_invalid_or_unexpected_existing_index(monkeypatch, state):
    connection = PostgreSQLMigrationConnection(existing=state)
    postgres_engine(monkeypatch, connection)
    with pytest.raises(RuntimeError, match="idx_snapshots_latest"):
        migrate.main()
    assert connection.created == []


def test_postgresql_does_not_report_ready_after_unusable_build(monkeypatch, capsys):
    connection = PostgreSQLMigrationConnection(built=valid_index(valid=False))
    postgres_engine(monkeypatch, connection)
    with pytest.raises(RuntimeError, match="idx_snapshots_latest"):
        migrate.main()
    assert connection.created == ["idx_snapshots_latest"]
    assert not capsys.readouterr().out
