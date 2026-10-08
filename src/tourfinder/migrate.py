"""Explicit additive deployment migration; never removes or prunes data.

Run once before deploying application code. PostgreSQL builds the large
history index CONCURRENTLY outside a transaction. create_all does not add
indexes to existing tables; this command does.
"""
from . import db


def _postgres_index_state(connection, name, table):
    # Index names are unique only within their table's schema. An index in an
    # unrelated schema must neither satisfy nor block this migration.
    return connection.exec_driver_sql("""
        SELECT i.indisvalid AS valid, i.indisready AS ready,
               i.indrelid = target.oid AS correct_table,
               i.indpred IS NULL AND i.indexprs IS NULL AS plain,
               am.amname AS method, i.indoption::smallint[] AS options,
               ARRAY(
                   SELECT a.attname::text
                   FROM unnest(i.indkey) WITH ORDINALITY AS key(attnum, position)
                   JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=key.attnum
                   ORDER BY key.position
               ) AS columns
        FROM pg_class target
        JOIN pg_class c ON c.relnamespace=target.relnamespace AND c.relname=%s
        LEFT JOIN pg_index i ON i.indexrelid=c.oid
        LEFT JOIN pg_am am ON am.oid=c.relam
        WHERE target.oid=to_regclass(%s)
    """, (name, table)).mappings().first()


def _validate_postgres_index(state, name, columns):
    if not state or not state["valid"] or not state["ready"]:
        raise RuntimeError(
            f"Index {name} is absent or invalid from an earlier interrupted build; "
            "inspect and repair it before retrying.")
    expected = columns.split(",")
    if (not state["correct_table"] or not state["plain"]
            or state["method"] != "btree" or state["columns"] != expected
            or list(state["options"] or []) != [0] * len(expected)):
        raise RuntimeError(
            f"Index {name} has an unexpected definition; inspect it before retrying.")


def main():
    engine = db.get_engine()
    indexes = [
        ("idx_snapshots_latest", "price_snapshots", "offer_id,fetched_at,id"),
        ("idx_subscriptions_owner", "subscriptions", "owner_id,enabled"),
        ("idx_telegram_delivery_owner", "telegram_deliveries", "owner_id,sent_at"),
    ]
    if engine.dialect.name == "postgresql":
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            for name, table, columns in indexes:
                existing = _postgres_index_state(connection, name, table)
                if existing is not None:
                    _validate_postgres_index(existing, name, columns)
                connection.exec_driver_sql(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table}({columns})")
                # IF NOT EXISTS does not verify an existing index's definition.
                _validate_postgres_index(
                    _postgres_index_state(connection, name, table), name, columns)
    else:
        with engine.begin() as connection:
            for name, table, columns in indexes:
                connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})")
    print("Additive schema and indexes ready. No data was removed.")


if __name__ == "__main__":
    main()
