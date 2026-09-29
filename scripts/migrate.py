"""Apply SQL migrations to the database in $DATABASE_URL."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402

from app import DEFAULT_DATABASE_URL  # noqa: E402
from app.migrate import apply_migrations  # noqa: E402

if __name__ == "__main__":
    engine = create_engine(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    applied = apply_migrations(engine)
    print("Applied: " + ", ".join(applied) if applied else "Database is up to date.")
