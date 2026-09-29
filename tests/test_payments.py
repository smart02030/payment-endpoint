import threading
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import Cart, Payment
from app.provider import ChargeResult, ProviderError


def pay(client, user, cart_id, key=None, json=None, headers=None):
    all_headers = {"X-User-Id": str(user.id)}
    if key is not None:
        all_headers["Idempotency-Key"] = key
    all_headers.update(headers or {})
    return client.post(f"/carts/{cart_id}/payments", json=json, headers=all_headers)


def payments_for(db, cart):
    db.expire_all()
    return db.scalars(select(Payment).where(Payment.cart_id == cart.id)).all()


def cart_status(db, cart):
    db.expire_all()
    return db.get(Cart, cart.id).status


# --- happy path ---------------------------------------------------------------

def test_successful_payment_charges_total_and_checks_out_cart(client, db, factory, provider):
    user = factory.user()
    method = factory.payment_method(user, token="tok_ok")
    cart = factory.cart(user, items=[(1, "45.00"), (2, "12.50")])

    resp = pay(client, user, cart.id, key="k1")

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["status"] == "succeeded"
    assert body["amount"] == "70.00"
    assert body["currency"] == "USD"
    assert body["payment_method_id"] == str(method.id)
    assert body["provider_payment_id"].startswith("mock_ch_")
    assert resp.headers["Location"] == f"/payments/{body['id']}"

    assert provider.calls == [
        {"token": "tok_ok", "amount": Decimal("70.00"), "currency": "USD", "idempotency_key": body["id"]}
    ]
    [payment] = payments_for(db, cart)
    assert payment.status == "succeeded"
    assert cart_status(db, cart) == "checked_out"


def test_uses_explicit_payment_method(client, db, factory, provider):
    user = factory.user()
    factory.payment_method(user, token="tok_default", is_default=True)
    other = factory.payment_method(user, token="tok_other", is_default=False)
    cart = factory.cart(user)

    resp = pay(client, user, cart.id, json={"payment_method_id": str(other.id)})

    assert resp.status_code == 201
    assert resp.get_json()["payment_method_id"] == str(other.id)
    assert provider.calls[0]["token"] == "tok_other"


def test_payment_can_be_read_back(client, factory):
    user = factory.user()
    factory.payment_method(user)
    cart = factory.cart(user)
    created = pay(client, user, cart.id).get_json()

    resp = client.get(f"/payments/{created['id']}", headers={"X-User-Id": str(user.id)})
    assert resp.status_code == 200
    assert resp.get_json() == created

    stranger = factory.user()
    resp = client.get(f"/payments/{created['id']}", headers={"X-User-Id": str(stranger.id)})
    assert resp.status_code == 404


# --- declines and provider errors -----------------------------------------------

@pytest.mark.parametrize("token, code", [("tok_decline", "card_declined"), ("tok_insufficient", "insufficient_funds")])
def test_declined_payment_keeps_cart_active(client, db, factory, token, code):
    user = factory.user()
    factory.payment_method(user, token=token)
    cart = factory.cart(user)

    resp = pay(client, user, cart.id)

    assert resp.status_code == 402
    body = resp.get_json()
    assert body["status"] == "failed"
    assert body["failure_code"] == code
    assert cart_status(db, cart) == "active"


def test_can_retry_with_another_card_after_decline(client, db, factory):
    user = factory.user()
    factory.payment_method(user, token="tok_decline")
    good = factory.payment_method(user, token="tok_ok", is_default=False)
    cart = factory.cart(user)

    assert pay(client, user, cart.id, key="a").status_code == 402
    resp = pay(client, user, cart.id, key="b", json={"payment_method_id": str(good.id)})

    assert resp.status_code == 201
    assert sorted(p.status for p in payments_for(db, cart)) == ["failed", "succeeded"]
    assert cart_status(db, cart) == "checked_out"


def test_provider_error_leaves_payment_pending_and_retry_resolves_it(client, db, factory, provider):
    user = factory.user()
    factory.payment_method(user, token="tok_ok")
    cart = factory.cart(user)

    real_charge = provider.charge
    provider.charge = lambda **kw: (_ for _ in ()).throw(ProviderError("timeout"))
    resp = pay(client, user, cart.id, key="k")

    assert resp.status_code == 502
    first = resp.get_json()
    assert first["status"] == "pending"
    assert cart_status(db, cart) == "active"

    # A different key must not start a second charge while the first is unresolved.
    resp = pay(client, user, cart.id, key="other")
    assert resp.status_code == 409
    assert resp.get_json()["error"] == {
        "code": "payment_in_progress",
        "message": "Another payment for this cart is in progress.",
        "payment_id": first["id"],
    }

    # Retrying with the same key re-sends the same provider idempotency key.
    provider.charge = real_charge
    resp = pay(client, user, cart.id, key="k")

    assert resp.status_code == 201
    assert resp.get_json()["id"] == first["id"]
    assert resp.get_json()["status"] == "succeeded"
    assert provider.calls[-1]["idempotency_key"] == first["id"]
    assert len(payments_for(db, cart)) == 1
    assert cart_status(db, cart) == "checked_out"


# --- idempotency ------------------------------------------------------------------

def test_same_idempotency_key_returns_first_result_without_charging_again(client, db, factory, provider):
    user = factory.user()
    factory.payment_method(user)
    cart = factory.cart(user)

    first = pay(client, user, cart.id, key="same")
    second = pay(client, user, cart.id, key="same")

    assert first.status_code == second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert first.get_json() == second.get_json()
    assert len(provider.calls) == 1
    assert len(payments_for(db, cart)) == 1


def test_replayed_decline_returns_402_again(client, factory, provider):
    user = factory.user()
    factory.payment_method(user, token="tok_decline")
    cart = factory.cart(user)

    assert pay(client, user, cart.id, key="k").status_code == 402
    resp = pay(client, user, cart.id, key="k")

    assert resp.status_code == 402
    assert resp.headers["Idempotent-Replayed"] == "true"
    assert len(provider.calls) == 1


def test_idempotency_key_reused_for_another_cart_is_rejected(client, factory):
    user = factory.user()
    factory.payment_method(user)
    cart_a = factory.cart(user)
    cart_b = factory.cart(user)

    assert pay(client, user, cart_a.id, key="k").status_code == 201
    resp = pay(client, user, cart_b.id, key="k")

    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "idempotency_key_reused"


def test_idempotency_keys_are_scoped_per_user(client, factory):
    alice, bob = factory.user(), factory.user()
    factory.payment_method(alice)
    factory.payment_method(bob)

    assert pay(client, alice, factory.cart(alice).id, key="k").status_code == 201
    assert pay(client, bob, factory.cart(bob).id, key="k").status_code == 201


# --- cart and payment-method validation ------------------------------------------

def test_second_payment_for_paid_cart_is_rejected(client, factory, provider):
    user = factory.user()
    factory.payment_method(user)
    cart = factory.cart(user)

    assert pay(client, user, cart.id, key="a").status_code == 201
    resp = pay(client, user, cart.id, key="b")

    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "cart_not_active"
    assert len(provider.calls) == 1


@pytest.mark.parametrize("status", ["checked_out", "abandoned"])
def test_inactive_cart_is_rejected(client, factory, provider, status):
    user = factory.user()
    factory.payment_method(user)
    cart = factory.cart(user, status=status)

    resp = pay(client, user, cart.id)

    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "cart_not_active"
    assert provider.calls == []


def test_empty_cart_is_rejected(client, factory, provider):
    user = factory.user()
    factory.payment_method(user)
    cart = factory.cart(user, items=[])

    resp = pay(client, user, cart.id)

    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "cart_empty"
    assert provider.calls == []


def test_cart_with_mixed_currencies_is_rejected(client, factory):
    user = factory.user()
    factory.payment_method(user)
    cart = factory.cart(user, items=[(1, "10.00")])
    other = factory.cart(user, items=[(1, "10.00")], currency="EUR")
    # Move the EUR item into the first cart.
    factory.session.execute(text("UPDATE cart_items SET cart_id = :a WHERE cart_id = :b"), {"a": cart.id, "b": other.id})
    factory.session.commit()

    resp = pay(client, user, cart.id)

    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "total_unavailable"


def test_unknown_cart_returns_404(client, factory):
    user = factory.user()
    resp = pay(client, user, uuid.uuid4())
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "cart_not_found"


def test_other_users_cart_returns_404(client, factory, provider):
    owner, stranger = factory.user(), factory.user()
    factory.payment_method(stranger)
    cart = factory.cart(owner)

    resp = pay(client, stranger, cart.id)

    assert resp.status_code == 404
    assert provider.calls == []


def test_user_without_default_payment_method_is_rejected(client, factory):
    user = factory.user()
    factory.payment_method(user, is_default=False)
    cart = factory.cart(user)

    resp = pay(client, user, cart.id)

    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "no_payment_method"


def test_other_users_payment_method_is_rejected(client, factory, provider):
    user, stranger = factory.user(), factory.user()
    factory.payment_method(user)
    stolen = factory.payment_method(stranger, token="tok_stranger")
    cart = factory.cart(user)

    resp = pay(client, user, cart.id, json={"payment_method_id": str(stolen.id)})

    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "payment_method_not_found"
    assert provider.calls == []


# --- request validation ------------------------------------------------------------

@pytest.mark.parametrize("header", [None, "", "not-a-uuid"])
def test_missing_or_invalid_user_header_returns_401(client, factory, header):
    user = factory.user()
    cart = factory.cart(user)
    headers = {} if header is None else {"X-User-Id": header}

    resp = client.post(f"/carts/{cart.id}/payments", headers=headers)

    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "unauthenticated"


def test_invalid_json_body_returns_400(client, factory):
    user = factory.user()
    cart = factory.cart(user)
    resp = client.post(
        f"/carts/{cart.id}/payments",
        data="{not json",
        headers={"X-User-Id": str(user.id), "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_body"


def test_invalid_payment_method_id_returns_400(client, factory):
    user = factory.user()
    cart = factory.cart(user)
    resp = pay(client, user, cart.id, json={"payment_method_id": "nope"})
    assert resp.status_code == 400


def test_too_long_idempotency_key_returns_400(client, factory):
    user = factory.user()
    cart = factory.cart(user)
    resp = pay(client, user, cart.id, key="x" * 256)
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_idempotency_key"


def test_malformed_cart_id_returns_404(client, factory):
    user = factory.user()
    resp = client.post("/carts/not-a-uuid/payments", headers={"X-User-Id": str(user.id)})
    assert resp.status_code == 404


def test_index_lists_endpoints(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "POST /carts/{cart_id}/payments" in resp.get_json()["endpoints"]


def test_health_checks_database(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok", "database": "ok"}


# --- concurrency and schema guarantees ----------------------------------------------

class SlowProvider:
    """Succeeds after a delay so concurrent requests overlap."""

    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def charge(self, **kwargs):
        with self.lock:
            self.calls += 1
        time.sleep(0.3)
        return ChargeResult(True, f"slow_{uuid.uuid4().hex}")


def test_concurrent_requests_charge_the_cart_only_once(app, db, factory):
    provider = SlowProvider()
    app.extensions["payment_provider"] = provider
    user = factory.user()
    factory.payment_method(user)
    cart = factory.cart(user)

    statuses = []

    def worker(i):
        resp = pay(app.test_client(), user, cart.id, key=f"key-{i}")
        statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses) == [201, 409, 409, 409, 409]
    assert provider.calls == 1
    assert [p.status for p in payments_for(db, cart)] == ["succeeded"]


def test_database_rejects_two_open_payments_for_one_cart(db, factory):
    user = factory.user()
    method = factory.payment_method(user)
    cart = factory.cart(user)

    def pending(key):
        return Payment(
            cart_id=cart.id, user_id=user.id, payment_method_id=method.id,
            amount=Decimal("10.00"), currency="USD", status="pending", idempotency_key=key,
        )

    db.add(pending("a"))
    db.commit()
    db.add(pending("b"))
    with pytest.raises(IntegrityError, match="uq_payments_cart_open"):
        db.commit()
    db.rollback()


def test_database_requires_failure_code_on_failed_payments(db, factory):
    user = factory.user()
    method = factory.payment_method(user)
    cart = factory.cart(user)

    db.add(Payment(
        cart_id=cart.id, user_id=user.id, payment_method_id=method.id,
        amount=Decimal("10.00"), currency="USD", status="failed", idempotency_key="k",
    ))
    with pytest.raises(IntegrityError, match="ck_payments_failure_code_iff_failed"):
        db.commit()
    db.rollback()
