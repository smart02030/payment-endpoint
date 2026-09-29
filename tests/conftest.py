import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.migrate import apply_migrations
from app.models import Cart, CartItem, Product, User, UserPaymentMethod
from app.provider import MockPaymentProvider

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/payments_test"
)

TABLES = ["payments", "user_payment_methods", "cart_items", "carts", "products", "users"]


@pytest.fixture(scope="session")
def engine():
    engine = create_engine(TEST_DATABASE_URL)
    # Start every test run from a clean schema, built by the real migrations.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    apply_migrations(engine)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(engine):
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))


@pytest.fixture
def provider():
    return MockPaymentProvider()


@pytest.fixture
def app(engine, provider):
    app = create_app(database_url=TEST_DATABASE_URL, provider=provider)
    app.config["TESTING"] = True
    yield app
    app.extensions["db_engine"].dispose()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(engine):
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        yield session


class Factory:
    def __init__(self, session):
        self.session = session

    def _save(self, obj):
        self.session.add(obj)
        self.session.commit()
        return obj

    def user(self):
        return self._save(User(email=f"{uuid.uuid4().hex}@example.com", name="Test User"))

    def product(self, price="10.00", currency="USD"):
        return self._save(Product(name="Thing", price=Decimal(price), currency=currency, stock_quantity=10))

    def cart(self, user, status="active", items=((1, "10.00"),), currency="USD"):
        cart = self._save(Cart(user_id=user.id, status=status))
        for quantity, price in items:
            product = self.product(price, currency)
            self._save(CartItem(cart_id=cart.id, product_id=product.id, quantity=quantity, unit_price=Decimal(price)))
        return cart

    def payment_method(self, user, token="tok_test_visa", is_default=True):
        return self._save(
            UserPaymentMethod(user_id=user.id, provider_token=token, last_four="4242", is_default=is_default)
        )


@pytest.fixture
def factory(db):
    return Factory(db)
