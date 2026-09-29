# Payment Endpoint

A Flask + SQLAlchemy + PostgreSQL service that starts the payment for a user's cart.

```
POST /carts/{cart_id}/payments
GET  /payments/{payment_id}
GET  /health          database connectivity check
GET  /                lists the endpoints and the sample user/cart ids
```

## Running it

Requirements: Python 3.10+ and PostgreSQL 13+, installed natively or run with Docker.

### 1. Get PostgreSQL

**Option A: native install (no Docker).** On Windows:

```powershell
winget install --id PostgreSQL.PostgreSQL.16 --silent --override `
  "--mode unattended --unattendedmodeui none --superpassword postgres --serverport 5432 --enable-components server,commandlinetools --disable-components pgAdmin,stackbuilder"
```

This installs PostgreSQL as the `postgresql-x64-16` Windows service, which starts
automatically. On macOS use `brew install postgresql@16 && brew services start postgresql@16`.
On Debian/Ubuntu use `sudo apt install postgresql`. Then create the two databases:

```bash
# Windows: "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -h localhost
psql -U postgres -h localhost -c "CREATE DATABASE payments;" -c "CREATE DATABASE payments_test;"
```

If your `postgres` user has a different password (or you use another user), set
`DATABASE_URL` / `TEST_DATABASE_URL` accordingly (see below).

**Option B: Docker.** `docker compose up -d db` starts PostgreSQL 16 and creates both databases.

### 2. Install, migrate, run

```bash
# Install dependencies
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Apply migrations (base schema + payments table, includes sample data)
python scripts/migrate.py

# Run the server on http://localhost:5000
flask --app wsgi run
```

The database URL defaults to `postgresql+psycopg://postgres:postgres@localhost:5432/payments`.
Override it with the `DATABASE_URL` environment variable.

### Try it with the sample data

```bash
curl -i -X POST http://localhost:5000/carts/c1c1c1c1-c1c1-c1c1-c1c1-c1c1c1c1c1c1/payments \
  -H "X-User-Id: 11111111-1111-1111-1111-111111111111" \
  -H "Idempotency-Key: demo-1"
```

This returns `201` with a `succeeded` payment of `70.00 USD`, and the cart becomes
`checked_out`. Send the same request again and you get the same payment back, with an
`Idempotent-Replayed: true` header and no second charge.

### Try it with Postman

`postman/payment-endpoint.postman_collection.json` has 15 requests with automatic checks:
success, idempotent retry, declined card, provider timeout, access and validation errors.

1. Reset the database with the sample data plus demo users (Bob, whose default card is
   declined, and Carol, whose card makes the provider time out). **This drops the
   `payments` database.**
   ```bash
   python scripts/reset_demo_db.py
   ```
2. Start the app: `flask --app wsgi run`
3. In Postman: **Import** → choose the collection file → **Run collection**. Run the
   requests in order, because later ones use ids saved by earlier ones. Reset the
   database again before the next run.

Or run it from the command line: `npx newman run postman/payment-endpoint.postman_collection.json`

## Running the tests

The tests use a real PostgreSQL database. **They drop and recreate the `public` schema
of the test database**, then build it with the migrations in `migrations/`.

With PostgreSQL running and the `payments_test` database created (step 1 above):

```bash
pytest
```

The test database URL defaults to
`postgresql+psycopg://postgres:postgres@localhost:5432/payments_test`.
Override it with `TEST_DATABASE_URL`.

## Project layout

```
migrations/001_base_schema.sql   the given base schema, unchanged
migrations/002_payments.sql      the payments table (my part)
app/routes.py                    HTTP layer: headers, body parsing, status codes
app/payments.py                  the payment flow (reserve -> charge -> record)
app/provider.py                  mock payment provider
app/totals.py                    stand-in for the existing total calculation service
app/models.py                    SQLAlchemy mappings
app/migrate.py                   tiny SQL migration runner
tests/                           endpoint tests
```

## The API

### `POST /carts/{cart_id}/payments`

| Header            | Required | Meaning                                                                 |
|-------------------|----------|-------------------------------------------------------------------------|
| `X-User-Id`       | yes      | The authenticated user (see assumptions).                               |
| `Idempotency-Key` | no       | Any string up to 255 chars. Retries with the same key never charge twice. |

Body (optional JSON):

```json
{ "payment_method_id": "uuid" }
```

If `payment_method_id` is left out, the user's default payment method is used.

Response body is the payment:

```json
{
  "id": "…", "cart_id": "…", "payment_method_id": "…",
  "amount": "70.00", "currency": "USD",
  "status": "succeeded",
  "provider_payment_id": "mock_ch_…",
  "failure_code": null, "failure_message": null,
  "created_at": "…", "updated_at": "…"
}
```

| Status | When                                                                                       |
|--------|--------------------------------------------------------------------------------------------|
| 201    | Payment `succeeded`. Cart is now `checked_out`.                                            |
| 402    | Payment `failed`: the provider declined the card (`failure_code` says why). Cart stays `active`, so the user can try again. |
| 502    | Payment still `pending`: the provider call failed in an unknown way. Retry with the same `Idempotency-Key`. |
| 400    | Bad body, bad `payment_method_id`, or bad `Idempotency-Key`.                               |
| 401    | `X-User-Id` missing or not a UUID.                                                         |
| 404    | Cart does not exist or belongs to another user.                                            |
| 409    | `cart_not_active` (already paid or abandoned) or `payment_in_progress` (another attempt for this cart is still pending). |
| 422    | `cart_empty`, `no_payment_method`, `payment_method_not_found`, `idempotency_key_reused` (key already used for another cart), `total_unavailable`. |

Errors look like `{"error": {"code": "cart_not_active", "message": "…"}}`.

A replay of an idempotent request returns the same status code as the first response,
plus `Idempotent-Replayed: true`.

### `GET /payments/{payment_id}`

Returns the payment if it belongs to the user in `X-User-Id`, else `404`.

## Design

### Schema

`migrations/002_payments.sql` adds one table, `payments`. Each row is **one attempt**
to charge a card for a cart. A cart can have several attempts (for example declined,
then paid with another card), which keeps a full history.

Key points:

- `amount` and `currency` are copied from the total service when the attempt starts.
  They record exactly what we asked the provider to charge.
- `status` is `pending` → `succeeded` | `failed`.
- A **partial unique index** `uq_payments_cart_open ON payments(cart_id) WHERE status IN
  ('pending', 'succeeded')` makes the database refuse a second open or successful payment
  for the same cart. It is the last line of defence against a double charge, even if
  application code has a bug.
- `UNIQUE (user_id, idempotency_key)` makes client retries safe.
- CHECK constraints keep rows consistent: a succeeded payment has a
  `provider_payment_id`, and `failure_code` is set exactly when the status is `failed`.
- The card token is **not** copied into `payments`. The row points to
  `user_payment_methods` instead.

### Flow

The provider call is slow and remote, so it runs **outside** any database transaction:

1. **Reserve** (one transaction). Lock the cart row with `SELECT … FOR UPDATE`. Check
   that the cart is owned by the user, `active`, not already being paid, and not empty.
   Resolve the payment method, ask the total service for the amount, and insert a
   `pending` payment.
2. **Charge.** Call the provider with the token and amount. The payment id is the
   provider's idempotency key.
3. **Record** (one transaction). Save the result. On success, set the cart to
   `checked_out` in the **same** transaction as the payment update.

Concurrent requests for the same cart wait on the cart lock. They then see the
`pending` payment and get `409`. A test fires 5 requests at once and checks that the
card is charged exactly once.

### Unknown provider outcomes

A timeout or 5xx from the provider does not tell us whether the card was charged. So the
payment stays `pending` and the endpoint returns `502`, instead of marking it `failed`
and letting the user try again (which could charge twice). A retry with the **same**
`Idempotency-Key` sends the **same** provider idempotency key, so the provider returns the
original result and the payment is resolved. Meanwhile, a request with a different key
gets `409 payment_in_progress`, with the pending payment's id.

### Mock provider

`MockPaymentProvider` picks the outcome from the card token:

| Token contains | Result                                      |
|----------------|---------------------------------------------|
| `insufficient` | declined, `insufficient_funds`              |
| `decline`      | declined, `card_declined`                   |
| `error`        | raises `ProviderError` (simulated timeout)  |
| anything else  | success                                     |

The sample user's `tok_test_alice_visa` succeeds. Like a real provider, the mock honours
idempotency keys. It is injected into `create_app()`, so tests can replace it.

## Assumptions

- **Authentication is out of scope.** A gateway in front of the service authenticates
  the user and passes their id in `X-User-Id`. A cart owned by someone else returns
  `404`, not `403`, so the endpoint does not reveal that the cart exists.
- **"Starts the payment" means a synchronous charge.** The endpoint calls the provider
  and returns the result. There is no 3-D Secure / redirect step or webhook, because the
  task does not mention them.
- **The total calculation service exists elsewhere.** `app/totals.py` stands in for it.
  It sums `quantity × unit_price`, and it rejects carts that mix currencies. The real
  service would add tax, shipping and discounts. Its result is stored on the payment.
- **One currency per cart.** A cart that mixes currencies returns `422`.
- **Payment method:** the one given in the body (it must belong to the user), else the
  user's default one. If the user has several defaults, the newest is used. If there is
  no default and none is given, the endpoint returns `422` rather than guessing.
- **Idempotency-Key is optional.** Without it, each request is a new attempt, but the
  one-open-payment-per-cart rule still prevents a double charge. Keys are scoped per user.
- **Stock is not reserved or decremented.** Inventory and fulfilment are assumed to be
  another part of the system, which reacts to the cart being `checked_out`.
- **Amounts are decimals** (`NUMERIC(12,2)`), sent to the provider as `Decimal`. A real
  provider client would convert to minor units (cents).
- **The base schema is unchanged.** The payment part is a new migration, as the task asks.

## What I would add for production

- A reconciliation job that finds payments stuck in `pending` and asks the provider
  for their real status.
- Provider webhooks, 3-D Secure / SCA, refunds.
- Real authentication, rate limiting, structured logging and metrics.
- Alembic instead of the minimal SQL migration runner.
