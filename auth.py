"""Logins, role checks, and CSRF protection shared by every blueprint."""
from __future__ import annotations

import secrets
from datetime import datetime
from functools import wraps

from flask import abort, flash, g, redirect, request, session, url_for
from markupsafe import Markup

from models import User, db

# --- Auth -------------------------------------------------------------------

def current_user() -> User | None:
    if "user" not in g:
        user_id = session.get("user_id")
        user = db.session.get(User, user_id) if user_id else None
        g.user = user if (user and user.active) else None
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("public.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def customer_required(view):
    """Portal pages: a logged-in customer whose company is still active.
    Staff are sent to the backend instead — they have no prices of their own."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("public.login", next=request.path))
        if user.is_admin:
            return redirect(url_for("admin.dashboard"))
        if not (user.customer and user.customer.active):
            session.clear()
            flash("This account is no longer active. Please contact TriClean Supply.")
            return redirect(url_for("public.login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("public.login", next=request.path))
        if not user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def log_in(user: User) -> None:
    session.clear()  # new session id on login, so a pre-login session can't be fixated
    session["user_id"] = user.id
    g.user = user
    session.permanent = True
    user.last_login_at = datetime.utcnow()
    db.session.commit()


# --- CSRF -------------------------------------------------------------------
# Every POST must carry the per-session token rendered by csrf_field() in the
# templates. Small hand-rolled version rather than a new dependency.

def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def register(app) -> None:
    @app.context_processor
    def inject_user():
        return {"current_user": current_user()}

    @app.before_request
    def reset_user_cache():
        # current_user() caches on g; g can outlive a request when an app
        # context is already pushed (tests, CLI), so never trust a cached
        # user from before this request — e.g. "nobody" from before login.
        g.pop("user", None)

    @app.before_request
    def check_csrf():
        if request.method == "POST" and not app.config.get("TESTING_SKIP_CSRF"):
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
                abort(400, "Your session expired — go back, refresh the page, and try again.")

    @app.context_processor
    def inject_csrf():
        return {"csrf_field": lambda: Markup(f'<input type="hidden" name="_csrf" value="{csrf_token()}">')}
