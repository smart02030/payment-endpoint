import uuid

from flask import Blueprint, current_app, jsonify, request

from .models import PaymentStatus
from .payments import PaymentError, get_payment, start_payment

bp = Blueprint("payments", __name__)

MAX_IDEMPOTENCY_KEY_LENGTH = 255

# succeeded -> the card was charged; failed -> the provider declined it;
# pending -> the provider call failed in an unknown way, retry with the same key.
STATUS_TO_HTTP = {
    PaymentStatus.SUCCEEDED: 201,
    PaymentStatus.FAILED: 402,
    PaymentStatus.PENDING: 502,
}


@bp.get("/")
def index():
    return jsonify({
        "service": "payment-endpoint",
        "endpoints": {
            "POST /carts/{cart_id}/payments": "Start the payment for a cart. "
            "Headers: X-User-Id (required), Idempotency-Key (optional). "
            "Body (optional): {\"payment_method_id\": \"uuid\"}",
            "GET /payments/{payment_id}": "Read a payment. Header: X-User-Id",
            "GET /health": "Service and database health",
        },
        "sample_data": {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "cart_id": "c1c1c1c1-c1c1-c1c1-c1c1-c1c1c1c1c1c1",
        },
    })


@bp.get("/health")
def health():
    try:
        with current_app.extensions["db_engine"].connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception:
        return jsonify({"status": "error", "database": "unreachable"}), 503
    return jsonify({"status": "ok", "database": "ok"})


@bp.post("/carts/<uuid:cart_id>/payments")
def create_payment(cart_id: uuid.UUID):
    user_id = _current_user_id()
    idempotency_key = _idempotency_key()

    body = request.get_json(silent=True) if request.data else {}
    if not isinstance(body, dict):
        raise PaymentError(400, "invalid_body", "Request body must be a JSON object.")
    payment_method_id = _optional_uuid(body.get("payment_method_id"), "payment_method_id")

    outcome = start_payment(
        current_app.extensions["db_sessionmaker"],
        current_app.extensions["payment_provider"],
        user_id=user_id,
        cart_id=cart_id,
        idempotency_key=idempotency_key,
        payment_method_id=payment_method_id,
    )

    response = jsonify(outcome.payment.to_dict())
    response.status_code = STATUS_TO_HTTP[outcome.payment.status]
    response.headers["Location"] = f"/payments/{outcome.payment.id}"
    if outcome.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return response


@bp.get("/payments/<uuid:payment_id>")
def read_payment(payment_id: uuid.UUID):
    payment = get_payment(current_app.extensions["db_sessionmaker"], _current_user_id(), payment_id)
    return jsonify(payment.to_dict())


def _current_user_id() -> uuid.UUID:
    # Authentication is out of scope: an upstream gateway is assumed to have
    # verified the user and to pass their id in this header.
    raw = request.headers.get("X-User-Id")
    try:
        return uuid.UUID(raw)
    except (TypeError, ValueError):
        raise PaymentError(401, "unauthenticated", "A valid X-User-Id header is required.")


def _idempotency_key() -> str:
    key = request.headers.get("Idempotency-Key")
    if key is None:
        # Without a client key, retries are not deduplicated.
        return str(uuid.uuid4())
    key = key.strip()
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise PaymentError(
            400, "invalid_idempotency_key", f"Idempotency-Key must be 1-{MAX_IDEMPOTENCY_KEY_LENGTH} characters."
        )
    return key


def _optional_uuid(value, field: str):
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise PaymentError(400, "invalid_body", f"{field} must be a UUID.")
