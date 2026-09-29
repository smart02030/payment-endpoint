"""Starting a payment for a cart.

The flow has three steps, and the provider call sits outside any database
transaction so we never hold row locks while waiting on the network:

1. Reserve (one transaction). Lock the cart row, validate, take the total from
   the total calculation service and insert a ``pending`` payment. The lock
   plus the ``uq_payments_cart_open`` index mean only one open attempt per
   cart can exist.
2. Charge. Call the provider with the payment id as its idempotency key.
3. Record (one transaction). Store the result. On success mark the cart
   ``checked_out`` in the same transaction.

If the provider call fails in an unknown way (timeout, 5xx), we cannot know if
the card was charged, so the payment stays ``pending``. Retrying the request
with the same Idempotency-Key calls the provider again with the same provider
idempotency key, which is safe and resolves the payment.
"""
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Cart, CartStatus, Payment, PaymentStatus, UserPaymentMethod
from .provider import ChargeResult, PaymentProvider, ProviderError
from .totals import TotalCalculationError, calculate_cart_total


class PaymentError(Exception):
    def __init__(self, http_status: int, code: str, message: str, payment_id: Optional[uuid.UUID] = None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.payment_id = payment_id


@dataclass
class PaymentOutcome:
    payment: Payment
    replayed: bool = False


@dataclass
class _Reserved:
    payment: Payment
    token: str
    resumed: bool


def start_payment(
    session_factory: Callable[[], Session],
    provider: PaymentProvider,
    *,
    user_id: uuid.UUID,
    cart_id: uuid.UUID,
    idempotency_key: str,
    payment_method_id: Optional[uuid.UUID] = None,
) -> PaymentOutcome:
    reserved = _reserve(session_factory, user_id, cart_id, idempotency_key, payment_method_id)
    if isinstance(reserved, PaymentOutcome):
        return reserved

    payment = reserved.payment
    try:
        result: Optional[ChargeResult] = provider.charge(
            token=reserved.token,
            amount=payment.amount,
            currency=payment.currency.strip(),
            idempotency_key=str(payment.id),
        )
    except ProviderError:
        result = None

    return PaymentOutcome(_record(session_factory, payment.id, result), replayed=reserved.resumed)


def get_payment(session_factory: Callable[[], Session], user_id: uuid.UUID, payment_id: uuid.UUID) -> Payment:
    with session_factory() as session:
        payment = session.scalar(select(Payment).where(Payment.id == payment_id, Payment.user_id == user_id))
    if payment is None:
        raise PaymentError(404, "payment_not_found", "Payment not found.")
    return payment


def _reserve(session_factory, user_id, cart_id, idempotency_key, payment_method_id):
    with session_factory() as session:
        try:
            with session.begin():
                return _reserve_in_tx(session, user_id, cart_id, idempotency_key, payment_method_id)
        except IntegrityError:
            # A concurrent request inserted a payment for the same cart or with
            # the same idempotency key between our checks and our insert.
            pass

        with session.begin():
            existing = _find_by_key(session, user_id, idempotency_key)
            if existing is not None and existing.cart_id == cart_id:
                return _existing_outcome(session, existing)
        raise PaymentError(409, "payment_in_progress", "Another payment for this cart is in progress.")


def _reserve_in_tx(session, user_id, cart_id, idempotency_key, payment_method_id):
    cart = session.scalar(select(Cart).where(Cart.id == cart_id).with_for_update())
    if cart is None or cart.user_id != user_id:
        raise PaymentError(404, "cart_not_found", "Cart not found.")

    existing = _find_by_key(session, user_id, idempotency_key)
    if existing is not None:
        if existing.cart_id != cart_id:
            raise PaymentError(
                422, "idempotency_key_reused", "This Idempotency-Key was already used for a different cart."
            )
        return _existing_outcome(session, existing)

    if cart.status != CartStatus.ACTIVE:
        raise PaymentError(409, "cart_not_active", f"Cart is {cart.status} and cannot be paid.")

    open_payment = session.scalar(
        select(Payment).where(
            Payment.cart_id == cart_id,
            Payment.status.in_([PaymentStatus.PENDING, PaymentStatus.SUCCEEDED]),
        )
    )
    if open_payment is not None:
        raise PaymentError(
            409, "payment_in_progress", "Another payment for this cart is in progress.", open_payment.id
        )

    method = _resolve_payment_method(session, user_id, payment_method_id)

    try:
        total = calculate_cart_total(session, cart_id)
    except TotalCalculationError as exc:
        raise PaymentError(422, "total_unavailable", str(exc)) from exc
    if total.amount <= 0:
        raise PaymentError(422, "cart_empty", "Cart has nothing to pay for.")

    payment = Payment(
        cart_id=cart_id,
        user_id=user_id,
        payment_method_id=method.id,
        amount=total.amount,
        currency=total.currency,
        status=PaymentStatus.PENDING,
        idempotency_key=idempotency_key,
    )
    session.add(payment)
    session.flush()
    session.refresh(payment)
    return _Reserved(payment, method.provider_token, resumed=False)


def _existing_outcome(session, payment: Payment):
    """What to do when a request repeats an Idempotency-Key we have seen."""
    if payment.status != PaymentStatus.PENDING:
        return PaymentOutcome(payment, replayed=True)
    # Still pending: the first request is in flight or its provider call
    # failed in an unknown way. Charging again with the same provider
    # idempotency key is safe and settles it.
    token = session.scalar(
        select(UserPaymentMethod.provider_token).where(UserPaymentMethod.id == payment.payment_method_id)
    )
    return _Reserved(payment, token, resumed=True)


def _find_by_key(session, user_id, idempotency_key) -> Optional[Payment]:
    return session.scalar(
        select(Payment).where(Payment.user_id == user_id, Payment.idempotency_key == idempotency_key)
    )


def _resolve_payment_method(session, user_id, payment_method_id) -> UserPaymentMethod:
    query = select(UserPaymentMethod).where(UserPaymentMethod.user_id == user_id)
    if payment_method_id is not None:
        method = session.scalar(query.where(UserPaymentMethod.id == payment_method_id))
        if method is None:
            raise PaymentError(422, "payment_method_not_found", "Payment method not found.")
        return method

    method = session.scalar(
        query.where(UserPaymentMethod.is_default.is_(True))
        .order_by(UserPaymentMethod.created_at.desc())
        .limit(1)
    )
    if method is None:
        raise PaymentError(
            422, "no_payment_method", "No payment_method_id was given and the user has no default payment method."
        )
    return method


def _record(session_factory, payment_id: uuid.UUID, result: Optional[ChargeResult]) -> Payment:
    with session_factory() as session, session.begin():
        payment = session.scalar(select(Payment).where(Payment.id == payment_id).with_for_update())

        # A concurrent retry may have recorded the result already.
        if payment.status == PaymentStatus.PENDING and result is not None:
            payment.provider_payment_id = result.provider_payment_id
            if result.succeeded:
                payment.status = PaymentStatus.SUCCEEDED
                cart = session.scalar(select(Cart).where(Cart.id == payment.cart_id).with_for_update())
                cart.status = CartStatus.CHECKED_OUT
            else:
                payment.status = PaymentStatus.FAILED
                payment.failure_code = result.failure_code
                payment.failure_message = result.failure_message
            session.flush()
            session.refresh(payment)
        return payment
