"""Client for the payment total calculation service.

The task says this service already exists, so this module is a stand-in with
the interface we assume it has. It sums ``quantity * unit_price`` of the cart
items so the endpoint can be exercised end to end. Taxes, shipping and
discounts would live in the real service.
"""
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CartItem, Product


class TotalCalculationError(Exception):
    pass


@dataclass(frozen=True)
class CartTotal:
    amount: Decimal
    currency: str


def calculate_cart_total(session: Session, cart_id: uuid.UUID) -> CartTotal:
    rows = session.execute(
        select(CartItem.quantity, CartItem.unit_price, Product.currency)
        .join(Product, Product.id == CartItem.product_id)
        .where(CartItem.cart_id == cart_id)
    ).all()

    if not rows:
        return CartTotal(Decimal("0.00"), "USD")

    currencies = {currency.strip() for _, _, currency in rows}
    if len(currencies) > 1:
        raise TotalCalculationError(f"cart mixes currencies: {sorted(currencies)}")

    amount = sum((qty * price for qty, price, _ in rows), Decimal("0.00"))
    return CartTotal(amount.quantize(Decimal("0.01")), currencies.pop())
