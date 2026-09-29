import os
from typing import Optional

from flask import Flask, jsonify
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.exceptions import HTTPException

from .payments import PaymentError
from .provider import MockPaymentProvider, PaymentProvider

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/payments"


def create_app(database_url: Optional[str] = None, provider: Optional[PaymentProvider] = None) -> Flask:
    app = Flask(__name__)

    # pool_pre_ping replaces pooled connections that died (e.g. after a database
    # restart) instead of failing the next request with a 500.
    engine = create_engine(
        database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL), pool_pre_ping=True
    )
    # Objects are returned from the service after their session closes.
    app.extensions["db_sessionmaker"] = sessionmaker(engine, expire_on_commit=False)
    app.extensions["db_engine"] = engine
    app.extensions["payment_provider"] = provider or MockPaymentProvider()

    from .routes import bp
    app.register_blueprint(bp)

    @app.errorhandler(PaymentError)
    def handle_payment_error(exc: PaymentError):
        error = {"code": exc.code, "message": exc.message}
        if exc.payment_id is not None:
            error["payment_id"] = str(exc.payment_id)
        return jsonify({"error": error}), exc.http_status

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        code = (exc.name or "error").lower().replace(" ", "_")
        return jsonify({"error": {"code": code, "message": exc.description}}), exc.code

    return app
