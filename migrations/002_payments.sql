-- Payments for carts.
--
-- One row is one attempt to charge a card for a cart. A cart can have many
-- attempts over time (for example: declined, then succeeded with another
-- card), but at most one attempt can be in flight or successful.
--
-- Status values:
--   pending    — the row is created and the provider is being called.
--   succeeded  — the provider charged the card. The cart is checked_out.
--   failed     — the provider declined the charge or returned an error.
--
-- amount and currency are copied from the total calculation service when
-- the attempt starts. They are what we asked the provider to charge, so they
-- must not change if the cart or product prices change later.

CREATE TABLE payments (
    id                  UUID           PRIMARY KEY DEFAULT gen_random_uuid(),
    cart_id             UUID           NOT NULL REFERENCES carts(id),
    user_id             UUID           NOT NULL REFERENCES users(id),
    payment_method_id   UUID           NOT NULL REFERENCES user_payment_methods(id),
    amount              NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    currency            CHAR(3)        NOT NULL,
    status              TEXT           NOT NULL DEFAULT 'pending'
                                       CHECK (status IN ('pending', 'succeeded', 'failed')),
    -- Client-supplied key (Idempotency-Key header) so that a retried request
    -- returns the first attempt instead of charging the card twice.
    idempotency_key     TEXT           NOT NULL,
    -- The provider's id for the charge. Set when the provider accepted it.
    provider_payment_id TEXT,
    -- Why the attempt failed, e.g. card_declined, insufficient_funds,
    -- provider_error. Set only when status = 'failed'.
    failure_code        TEXT,
    failure_message     TEXT,
    created_at          TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ    NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_payments_user_idempotency_key UNIQUE (user_id, idempotency_key),
    CONSTRAINT ck_payments_succeeded_has_provider_id
        CHECK (status <> 'succeeded' OR provider_payment_id IS NOT NULL),
    CONSTRAINT ck_payments_failure_code_iff_failed
        CHECK ((status = 'failed') = (failure_code IS NOT NULL))
);

-- The last line of defence against double charging: the database refuses a
-- second pending or succeeded payment for the same cart, even if two
-- requests race past the application checks.
CREATE UNIQUE INDEX uq_payments_cart_open
    ON payments(cart_id)
    WHERE status IN ('pending', 'succeeded');

CREATE INDEX idx_payments_cart_id ON payments(cart_id);
CREATE INDEX idx_payments_user_id ON payments(user_id);
CREATE INDEX idx_payments_payment_method_id ON payments(payment_method_id);
