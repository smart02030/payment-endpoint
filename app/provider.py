"""Payment provider client (mocked).

``MockPaymentProvider`` decides the outcome from the card token, so tests and
manual runs are deterministic:

- token contains ``insufficient`` -> declined, ``insufficient_funds``
- token contains ``decline``      -> declined, ``card_declined``
- token contains ``error``        -> raises ``ProviderError`` (timeout / 5xx)
- anything else                   -> success

Like real providers it honours an idempotency key: charging again with the
same key returns the first result instead of charging twice.
"""
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Protocol


class ProviderError(Exception):
    """The provider could not be reached or returned an unexpected error."""


@dataclass(frozen=True)
class ChargeResult:
    succeeded: bool
    provider_payment_id: Optional[str] = None
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None


class PaymentProvider(Protocol):
    def charge(self, *, token: str, amount: Decimal, currency: str, idempotency_key: str) -> ChargeResult:
        ...


class MockPaymentProvider:
    def __init__(self) -> None:
        self._results: dict[str, ChargeResult] = {}
        self._lock = threading.Lock()
        self.calls: list[dict] = []

    def charge(self, *, token: str, amount: Decimal, currency: str, idempotency_key: str) -> ChargeResult:
        with self._lock:
            self.calls.append(
                {"token": token, "amount": amount, "currency": currency, "idempotency_key": idempotency_key}
            )
            if idempotency_key in self._results:
                return self._results[idempotency_key]

            if "error" in token:
                raise ProviderError("provider timed out")
            if "insufficient" in token:
                result = ChargeResult(False, f"mock_ch_{uuid.uuid4().hex}", "insufficient_funds", "Insufficient funds.")
            elif "decline" in token:
                result = ChargeResult(False, f"mock_ch_{uuid.uuid4().hex}", "card_declined", "The card was declined.")
            else:
                result = ChargeResult(True, f"mock_ch_{uuid.uuid4().hex}")

            self._results[idempotency_key] = result
            return result
