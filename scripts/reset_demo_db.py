"""Recreate the `payments` database with the sample data plus demo users.

WARNING: drops the database in $DATABASE_URL and builds it again.

Demo users (on top of Alice from the base schema's sample data):
  Bob   - default card is declined, second card succeeds
  Carol - card makes the mock provider time out
Used by the Postman collection in postman/.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app import DEFAULT_DATABASE_URL  # noqa: E402
from app.migrate import apply_migrations  # noqa: E402

DEMO_DATA = """
INSERT INTO users (id, email, name) VALUES
    ('33333333-3333-3333-3333-333333333333', 'carol@example.com', 'Carol');

INSERT INTO carts (id, user_id) VALUES
    ('b0b0b0b0-b0b0-b0b0-b0b0-b0b0b0b0b0b0', '22222222-2222-2222-2222-222222222222'),
    ('ca1ca1ca-ca1c-a1ca-1ca1-ca1ca1ca1ca1', '33333333-3333-3333-3333-333333333333');

INSERT INTO cart_items (cart_id, product_id, quantity, unit_price) VALUES
    ('b0b0b0b0-b0b0-b0b0-b0b0-b0b0b0b0b0b0', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 1, 89.99),
    ('ca1ca1ca-ca1c-a1ca-1ca1-ca1ca1ca1ca1', 'cccccccc-cccc-cccc-cccc-cccccccccccc', 4, 12.50);

INSERT INTO user_payment_methods (id, user_id, provider_token, last_four, is_default) VALUES
    ('b0b00000-0000-0000-0000-000000000001', '22222222-2222-2222-2222-222222222222', 'tok_test_bob_decline',    '0002', TRUE),
    ('b0b00000-0000-0000-0000-000000000002', '22222222-2222-2222-2222-222222222222', 'tok_test_bob_mastercard', '4444', FALSE),
    ('ca100000-0000-0000-0000-000000000001', '33333333-3333-3333-3333-333333333333', 'tok_test_carol_error',    '1111', TRUE);
"""

if __name__ == "__main__":
    url = make_url(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))

    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()

    engine = create_engine(url)
    apply_migrations(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(DEMO_DATA)
    engine.dispose()
    print(f"Database '{url.database}' reset: migrations + sample data + demo users (Bob, Carol).")
