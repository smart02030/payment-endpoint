"""A minimal SQL migration runner.

Applies every ``migrations/NNN_*.sql`` file in name order, once. Applied files
are recorded in ``schema_migrations`` so running it again is safe.
"""
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def apply_migrations(engine: Engine, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    applied_now = []
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        ))
        # Serialise concurrent runners.
        conn.execute(text("LOCK TABLE schema_migrations IN EXCLUSIVE MODE"))
        done = {row[0] for row in conn.execute(text("SELECT name FROM schema_migrations"))}

        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in done:
                continue
            # exec_driver_sql sends the file as-is, so multi-statement files work.
            conn.exec_driver_sql(path.read_text(encoding="utf-8"))
            conn.execute(text("INSERT INTO schema_migrations (name) VALUES (:n)"), {"n": path.name})
            applied_now.append(path.name)
    return applied_now
