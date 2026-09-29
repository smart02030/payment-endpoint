"""SQLAlchemy mappings.

The schema is owned by the SQL files in ``migrations/``; these classes only map
it. Columns this service does not use are left out of the existing tables.
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import ForeignKey, Numeric, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CartStatus:
    ACTIVE = "active"
    CHECKED_OUT = "checked_out"
    ABANDONED = "abandoned"


class PaymentStatus:
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    email: Mapped[str]
    name: Mapped[str]


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str]
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(server_default="USD")
    stock_quantity: Mapped[int] = mapped_column(server_default="0")


class Cart(Base):
    __tablename__ = "carts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(server_default=CartStatus.ACTIVE)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class CartItem(Base):
    __tablename__ = "cart_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    cart_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("carts.id"))
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int]
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class UserPaymentMethod(Base):
    __tablename__ = "user_payment_methods"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    provider_token: Mapped[str]
    last_four: Mapped[Optional[str]]
    is_default: Mapped[bool] = mapped_column(server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    cart_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("carts.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    payment_method_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_payment_methods.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=PaymentStatus.PENDING)
    idempotency_key: Mapped[str]
    provider_payment_id: Mapped[Optional[str]]
    failure_code: Mapped[Optional[str]]
    failure_message: Mapped[Optional[str]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "cart_id": str(self.cart_id),
            "payment_method_id": str(self.payment_method_id),
            "amount": str(self.amount),
            "currency": self.currency.strip(),
            "status": self.status,
            "provider_payment_id": self.provider_payment_id,
            "failure_code": self.failure_code,
            "failure_message": self.failure_message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
