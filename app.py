"""TriClean Supply — B2B ordering portal.

Three parts, one Flask app:
* public pages (public.py) — the marketing site and "open an account" form
* the customer portal (portal.py) — each customer logs in to see their own
  prices, order on account, and see their order history
* the admin backend (admin.py) — TriClean staff manage products, per-
  customer pricing, customers/logins, warehouse inventory, and orders
"""
from __future__ import annotations

import os
from datetime import timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

import auth

from models import User, db

load_dotenv()

LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///triclean.db")
    # Render/Neon hand out "postgres://"; SQLAlchemy wants "postgresql://",
    # and naming the psycopg2 driver avoids SQLAlchemy 2.1 defaulting to
    # psycopg v3, which isn't installed (same fix as job-agent).
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    app.config.update(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or "dev-only-not-secret",
        SQLALCHEMY_DATABASE_URI=_database_url(),
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 280},
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") != "development" and bool(os.environ.get("RENDER")),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # spreadsheet uploads
        COMPANY_NAME="TriClean Supply",
        COMPANY_PHONE=os.environ.get("COMPANY_PHONE", "(917) 582-9876"),
        COMPANY_EMAIL=os.environ.get("COMPANY_EMAIL", "tricleansupplycollc@yahoo.com"),
        COMPANY_ADDRESS=os.environ.get("COMPANY_ADDRESS", "5644 Netherland Ave, Bronx, NY 10471"),
    )
    if config:
        app.config.update(config)
    if app.config["SECRET_KEY"] == "dev-only-not-secret" and os.environ.get("RENDER"):
        raise RuntimeError("FLASK_SECRET_KEY must be set in production.")

    db.init_app(app)
    with app.app_context():
        db.create_all()

    from admin import bp as admin_bp
    from portal import bp as portal_bp
    from public import bp as public_bp
    app.register_blueprint(public_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(admin_bp)

    auth.register(app)
    _register_errors(app)
    _register_template_helpers(app)
    _register_cli(app)
    return app


# --- Templates --------------------------------------------------------------

def _register_template_helpers(app: Flask) -> None:
    @app.template_filter("money")
    def money(value) -> str:
        if value is None:
            return ""
        return f"${Decimal(value):,.2f}"

    @app.template_filter("date")
    def date(value, fmt="%b %d, %Y") -> str:
        # Stored as naive UTC; shown in New York time, where TriClean and
        # its customers are.
        if not value:
            return ""
        return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime(fmt)

    @app.context_processor
    def inject_company():
        return {"company": {k[8:].lower(): v for k, v in app.config.items() if k.startswith("COMPANY_")}}


# --- Errors -----------------------------------------------------------------

def _register_errors(app: Flask) -> None:
    from flask import render_template

    pages = {
        400: ("Something went wrong with that request", None),
        403: ("Not allowed", "Your login doesn't have access to that page."),
        404: ("Page not found", "That page doesn't exist, or you don't have access to it."),
        413: ("File too large", "Uploads are limited to 5 MB."),
        500: ("Something went wrong", "Please try again. If it keeps happening, let us know through the Contact us page."),
    }
    for code, (title, message) in pages.items():
        def handler(e, code=code, title=title, message=message):
            detail = message or getattr(e, "description", None) or "Please go back and try again."
            return render_template("error.html", code=code, title=title, message=detail), code
        app.register_error_handler(code, handler)


# --- CLI --------------------------------------------------------------------

def _register_cli(app: Flask) -> None:
    import click

    @app.cli.command("create-admin")
    @click.argument("email")
    @click.option("--name", default="")
    def create_admin(email, name):
        """Create (or re-invite) a staff login and print its set-password link."""
        email = email.strip().lower()
        user = User.query.filter_by(email=email).first() or User(email=email)
        user.role = "admin"
        user.name = name or user.name
        user.active = True
        token = user.new_invite()
        db.session.add(user)
        db.session.commit()
        base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000").rstrip("/")
        click.echo(f"Admin {email} ready. Set the password here (valid 7 days):\n{base}/set-password/{token}")
