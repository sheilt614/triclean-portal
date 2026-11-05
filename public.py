"""Public site: what anyone sees before logging in. Prices are never shown
here — they're per-customer, so the catalog only shows what's carried."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from auth import current_user, log_in
from models import AccountRequest, Product, User, db

bp = Blueprint("public", __name__)

BUSINESS_TYPES = (
    "Office park / commercial building", "School / university", "Hospital / healthcare",
    "Nursing home / senior living", "Hotel", "Real estate / property management",
    "Apartment / residential building", "Janitorial contractor", "Child care",
    "Bar / restaurant", "Other",
)


def _categories() -> list[str]:
    rows = db.session.query(Product.category).filter(Product.active.is_(True)).distinct().all()
    return sorted(c for (c,) in rows if c)


@bp.route("/")
def home():
    return render_template("public/home.html", categories=_categories())


@bp.route("/products")
def products():
    category = request.args.get("category") or None
    query = Product.query.filter_by(active=True)
    if category:
        query = query.filter_by(category=category)
    return render_template(
        "public/products.html", products=query.order_by(Product.category, Product.name).all(),
        categories=_categories(), category=category,
    )


@bp.route("/open-an-account", methods=["GET", "POST"])
def open_account():
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in
                ("business_name", "contact_name", "email", "phone", "business_type", "message")}
        if not form["business_name"] or not form["email"] or "@" not in form["email"]:
            flash("Please enter your business name and a valid email.")
            return render_template("public/open_account.html", form=form, business_types=BUSINESS_TYPES)
        db.session.add(AccountRequest(**{k: v or None for k, v in form.items()}))
        db.session.commit()
        return render_template("public/open_account_thanks.html", form=form)
    return render_template("public/open_account.html", form={}, business_types=BUSINESS_TYPES)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(_home_for(current_user()))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if user and user.active and user.check_password(request.form.get("password", "")):
            log_in(user)
            next_url = request.args.get("next", "")
            # Only follow same-site paths, never an absolute URL someone put in the link.
            if next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(_home_for(user))
        flash("That email and password don't match an active account.")
    return render_template("public/login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("public.home"))


@bp.route("/set-password/<token>", methods=["GET", "POST"])
def set_password(token):
    """One-time link from an invite or password reset (created by staff)."""
    user = User.query.filter_by(invite_token=token).first()
    if not user or not user.invite_expires_at or user.invite_expires_at < datetime.utcnow():
        return render_template("public/set_password.html", invalid=True), 404
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 8:
            flash("Use at least 8 characters.")
        elif password != request.form.get("confirm", ""):
            flash("The two passwords don't match.")
        else:
            user.set_password(password)
            user.invite_token = None
            user.invite_expires_at = None
            db.session.commit()
            log_in(user)
            flash("Password set — you're logged in.")
            return redirect(_home_for(user))
    return render_template("public/set_password.html", invalid=False, user=user)


def _home_for(user: User) -> str:
    return url_for("admin.dashboard") if user.is_admin else url_for("portal.catalog")
