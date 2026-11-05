"""Admin backend for TriClean staff: products, pricing (standard and per
customer, with bulk tiers), customers and their logins, warehouses and
inventory, orders, and account requests from the public site."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

import fulfillment
import imports
import mailer
from auth import admin_required, current_user
from models import (
    ORDER_STATUSES, ContactMessage, Customer, Inventory, Order, Price, Product,
    StockMovement, User, Warehouse, db,
)
from pricing import PriceBook

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _money_or_none(value: str | None) -> Decimal | None:
    value = (value or "").replace("$", "").replace(",", "").strip()
    if not value:
        return None
    try:
        amount = Decimal(value).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return amount if amount >= 0 else None


def _int_or_none(value: str | None) -> int | None:
    try:
        return int((value or "").strip())
    except ValueError:
        return None


def _invite_url(user: User) -> str:
    return url_for("public.set_password", token=user.invite_token, _external=True)


def _deliver_invite(user: User, what: str, reset: bool = False) -> None:
    """Emails the set-password link when email is set up; either way, shows
    the link so staff can still send it by hand if the email doesn't arrive."""
    link = _invite_url(user)
    if mailer.send_invite(user, link, reset=reset):
        flash(f"{what} Emailed the set-password link to {user.email}. "
              f"If it doesn't arrive, send them this link yourself (valid 7 days): {link}")
    else:
        flash(f"{what} Send {user.email} this link to set their password (valid 7 days): {link}")


def _csv_response(filename: str, body: str) -> Response:
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --- Dashboard ----------------------------------------------------------------

@bp.route("/")
@admin_required
def dashboard():
    open_orders = Order.query.filter(Order.status.in_(("submitted", "confirmed"))).order_by(Order.created_at).all()
    low_stock = (Inventory.query.join(Product).join(Warehouse)
                 .filter(Product.active.is_(True), Warehouse.active.is_(True),
                         Inventory.quantity <= Inventory.reorder_point)
                 .order_by(Inventory.quantity).limit(25).all())
    unpaid = Order.query.filter(Order.payment_method == "invoice", Order.paid.is_(False),
                                Order.status.in_(("shipped", "delivered"))).all()
    return render_template(
        "admin/dashboard.html", open_orders=open_orders, low_stock=low_stock,
        unpaid=unpaid, unpaid_total=sum((o.total for o in unpaid), Decimal(0)),
        messages=ContactMessage.query.filter_by(handled=False).count(),
        recent=Order.query.order_by(Order.created_at.desc()).limit(10).all(),
    )


# --- Products & standard pricing ------------------------------------------------

@bp.route("/products")
@admin_required
def products():
    q = request.args.get("q", "").strip()
    query = Product.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.name.ilike(like), Product.sku.ilike(like),
                                    Product.category.ilike(like), Product.brand.ilike(like)))
    return render_template("admin/products.html",
                           products=query.order_by(Product.active.desc(), Product.category, Product.name).all(), q=q)


@bp.route("/products/new", methods=["GET", "POST"])
@bp.route("/products/<int:product_id>", methods=["GET", "POST"])
@admin_required
def product_edit(product_id=None):
    product = db.session.get(Product, product_id) if product_id else Product(active=True, base_price=0)
    if product_id and not product:
        abort(404)
    if request.method == "POST":
        sku = request.form.get("sku", "").strip()
        name = request.form.get("name", "").strip()
        base_price = _money_or_none(request.form.get("base_price"))
        clash = Product.query.filter(db.func.lower(Product.sku) == sku.lower(), Product.id != (product.id or 0)).first()
        if not sku or not name or base_price is None:
            flash("SKU, name, and a valid standard price are required.")
        elif clash:
            flash(f"SKU {sku} is already used by {clash.name}.")
        else:
            product.sku, product.name, product.base_price = sku, name, base_price
            for field in ("category", "brand", "unit", "description", "image_url"):
                setattr(product, field, request.form.get(field, "").strip() or None)
            product.active = bool(request.form.get("active"))
            db.session.add(product)
            db.session.commit()
            flash(f"Saved {product.name}.")
            return redirect(url_for("admin.product_edit", product_id=product.id))

    tiers = customer_prices = stock = []
    if product.id:
        tiers = Price.query.filter_by(product_id=product.id, customer_id=None).order_by(Price.min_qty).all()
        customer_prices = (Price.query.filter(Price.product_id == product.id, Price.customer_id.isnot(None))
                           .join(Customer).order_by(Customer.name, Price.min_qty).all())
        by_wh = {i.warehouse_id: i for i in product.inventory}
        stock = [(w, by_wh.get(w.id)) for w in Warehouse.query.order_by(Warehouse.name).all()]
    return render_template("admin/product_edit.html", product=product, tiers=tiers,
                           customer_prices=customer_prices, stock=stock,
                           customers=Customer.query.filter_by(active=True).order_by(Customer.name).all())


@bp.route("/products/<int:product_id>/prices", methods=["POST"])
@admin_required
def product_price_add(product_id):
    """Adds or updates one price break — standard (no customer) or for a
    specific customer — from the product page."""
    product = db.session.get(Product, product_id) or abort(404)
    customer_id = _int_or_none(request.form.get("customer_id"))
    min_qty = _int_or_none(request.form.get("min_qty")) or 1
    price = _money_or_none(request.form.get("price"))
    if price is None or min_qty < 1:
        flash("Enter a valid price and a minimum quantity of at least 1.")
    elif customer_id is None and min_qty == 1:
        flash("The standard price for 1 is the product's standard price — change it in the form above.")
    else:
        if customer_id is not None and not db.session.get(Customer, customer_id):
            abort(400)
        row = Price.query.filter_by(product_id=product.id, customer_id=customer_id, min_qty=min_qty).first()
        if row:
            row.price = price
        else:
            db.session.add(Price(product_id=product.id, customer_id=customer_id, min_qty=min_qty, price=price))
        db.session.commit()
        flash("Price saved.")
    return redirect(request.form.get("back") or url_for("admin.product_edit", product_id=product.id))


@bp.route("/prices/<int:price_id>/delete", methods=["POST"])
@admin_required
def price_delete(price_id):
    row = db.session.get(Price, price_id) or abort(404)
    product_id = row.product_id
    db.session.delete(row)
    db.session.commit()
    flash("Price removed.")
    return redirect(request.form.get("back") or url_for("admin.product_edit", product_id=product_id))


# --- Customers, their prices, and logins ---------------------------------------

@bp.route("/customers")
@admin_required
def customers():
    return render_template("admin/customers.html",
                           customers=Customer.query.order_by(Customer.active.desc(), Customer.name).all())


@bp.route("/customers/new", methods=["GET", "POST"])
@bp.route("/customers/<int:customer_id>", methods=["GET", "POST"])
@admin_required
def customer_edit(customer_id=None):
    customer = db.session.get(Customer, customer_id) if customer_id else Customer(active=True)
    if customer_id and not customer:
        abort(404)
    from_message = _int_or_none(request.values.get("from_message")) if not customer_id else None
    lead = db.session.get(ContactMessage, from_message) if from_message else None
    if request.method == "GET" and lead:
        # Pre-fill from a Contact us message, including inviting the person who wrote in.
        customer.name = lead.organization or lead.name
        customer.contact_name, customer.email, customer.phone = lead.name, lead.email, lead.phone
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        discount = _money_or_none(request.form.get("discount_pct") or "0")
        tax_rate = _money_or_none(request.form.get("tax_rate") or "0")
        net_days = _int_or_none(request.form.get("net_days") or "30")
        if not name:
            flash("Business name is required.")
        elif (discount is None or discount > 100 or tax_rate is None or tax_rate > 100
              or net_days is None or net_days < 0):
            flash("Discount and tax rate must be 0–100, and net days a whole number.")
        else:
            customer.name = name
            for field in ("contact_name", "email", "phone", "billing_address", "shipping_address", "notes"):
                setattr(customer, field, request.form.get(field, "").strip() or None)
            customer.payment_terms = "card" if request.form.get("payment_terms") == "card" else "invoice"
            customer.net_days, customer.discount_pct = net_days, discount
            customer.tax_rate = Decimal(request.form.get("tax_rate") or "0").quantize(Decimal("0.001"))
            customer.active = bool(request.form.get("active"))
            is_new = customer.id is None
            invite_email = request.form.get("invite_email", "").strip().lower() if is_new else ""
            if invite_email and ("@" not in invite_email or User.query.filter_by(email=invite_email).first()):
                flash(f"Can't invite {invite_email}: " + ("it already has a login." if "@" in invite_email else "not a valid email."))
                return render_template("admin/customer_edit.html", customer=customer, from_message=from_message,
                                       invite_email=invite_email, invite_name=request.form.get("invite_name", ""))
            db.session.add(customer)
            db.session.flush()
            invited = None
            if invite_email:
                invited = User(email=invite_email, name=request.form.get("invite_name", "").strip() or None,
                               role="customer", customer_id=customer.id)
                invited.new_invite()
                db.session.add(invited)
            if lead:
                lead.handled = True
            db.session.commit()
            flash(f"Saved {customer.name}.")
            if invited:
                _deliver_invite(invited, "Invite ready.")
            return redirect(url_for("admin.customer_edit", customer_id=customer.id))
    return render_template("admin/customer_edit.html", customer=customer, from_message=from_message,
                           invite_email=(lead.email if lead else ""), invite_name=(lead.name if lead else ""))


@bp.route("/customers/<int:customer_id>/prices", methods=["GET", "POST"])
@admin_required
def customer_prices(customer_id):
    """Every product with this customer's effective price — the main place
    to set "customer A pays $500, customer B pays $450". Saving the grid
    sets each customer's own price for quantity 1; blank removes it."""
    customer = db.session.get(Customer, customer_id) or abort(404)
    products = Product.query.filter_by(active=True).order_by(Product.category, Product.name).all()
    if request.method == "POST":
        changed = 0
        for product in products:
            key = f"price_{product.id}"
            if key not in request.form:
                continue
            raw = request.form.get(key, "").strip()
            row = Price.query.filter_by(product_id=product.id, customer_id=customer.id, min_qty=1).first()
            if not raw:
                if row:
                    db.session.delete(row)
                    changed += 1
                continue
            price = _money_or_none(raw)
            if price is None:
                flash(f"Skipped {product.sku}: '{raw}' isn't a valid price.")
                continue
            if row and row.price != price:
                row.price = price
                changed += 1
            elif not row:
                db.session.add(Price(product_id=product.id, customer_id=customer.id, min_qty=1, price=price))
                changed += 1
        db.session.commit()
        flash(f"Updated {changed} price{'s' if changed != 1 else ''} for {customer.name}.")
        return redirect(url_for("admin.customer_prices", customer_id=customer.id))

    book = PriceBook(customer, products)
    own = {(r.product_id, r.min_qty): r for r in Price.query.filter_by(customer_id=customer.id).all()}
    rows = [{
        "product": p,
        "standard": book.standard_breaks(p)[0][1],
        "own_unit": own.get((p.id, 1)),
        "own_tiers": [r for (pid, q), r in sorted(own.items(), key=lambda kv: kv[0][1]) if pid == p.id and q > 1],
        "tiers": book.tiers(p),
    } for p in products]
    return render_template("admin/customer_prices.html", customer=customer, rows=rows)


@bp.route("/customers/<int:customer_id>/users", methods=["POST"])
@admin_required
def customer_user_add(customer_id):
    customer = db.session.get(Customer, customer_id) or abort(404)
    email = request.form.get("email", "").strip().lower()
    if not email or "@" not in email:
        flash("Enter a valid email.")
    elif User.query.filter_by(email=email).first():
        flash(f"{email} already has a login.")
    else:
        user = User(email=email, name=request.form.get("name", "").strip() or None,
                    role="customer", customer_id=customer.id)
        user.new_invite()
        db.session.add(user)
        db.session.commit()
        _deliver_invite(user, "Login created.")
    return redirect(url_for("admin.customer_edit", customer_id=customer.id))


@bp.route("/users/<int:user_id>/invite", methods=["POST"])
@admin_required
def user_reinvite(user_id):
    user = db.session.get(User, user_id) or abort(404)
    user.new_invite()
    user.active = True
    db.session.commit()
    _deliver_invite(user, "New set-password link created.", reset=bool(user.password_hash))
    return redirect(url_for("admin.customer_edit", customer_id=user.customer_id) if user.customer_id
                    else url_for("admin.staff"))


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def user_toggle(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user().id:
        flash("You can't deactivate your own login.")
    else:
        user.active = not user.active
        db.session.commit()
        flash(f"{user.email} {'reactivated' if user.active else 'deactivated'}.")
    return redirect(url_for("admin.customer_edit", customer_id=user.customer_id) if user.customer_id
                    else url_for("admin.staff"))


@bp.route("/users/<int:user_id>/email", methods=["POST"])
@admin_required
def user_change_email(user_id):
    """Moves a login to a new email address (e.g. from a personal address to
    a business one) — same account, password, and history; they log in with
    the new address from now on."""
    user = db.session.get(User, user_id) or abort(404)
    email = request.form.get("email", "").strip().lower()
    back = (url_for("admin.customer_edit", customer_id=user.customer_id) if user.customer_id
            else url_for("admin.staff"))
    if not email or "@" not in email:
        flash("Enter a valid email.")
    elif email == user.email:
        flash("That's already their email.")
    elif User.query.filter_by(email=email).first():
        flash(f"{email} already has a login.")
    else:
        old = user.email
        user.email = email
        db.session.commit()
        flash(f"Changed {old} to {email}. They log in with {email} from now on, with the same password.")
    return redirect(back)


@bp.route("/staff", methods=["GET", "POST"])
@admin_required
def staff():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email or "@" not in email:
            flash("Enter a valid email.")
        elif User.query.filter_by(email=email).first():
            flash(f"{email} already has a login.")
        else:
            user = User(email=email, name=request.form.get("name", "").strip() or None, role="admin")
            user.new_invite()
            db.session.add(user)
            db.session.commit()
            _deliver_invite(user, "Staff login created.")
        return redirect(url_for("admin.staff"))
    return render_template("admin/staff.html", staff=User.query.filter_by(role="admin").order_by(User.email).all())


# --- Warehouses & inventory --------------------------------------------------

@bp.route("/warehouses", methods=["GET", "POST"])
@admin_required
def warehouses():
    if request.method == "POST":
        warehouse_id = _int_or_none(request.form.get("id"))
        warehouse = db.session.get(Warehouse, warehouse_id) if warehouse_id else Warehouse()
        code = request.form.get("code", "").strip().upper()
        name = request.form.get("name", "").strip()
        clash = Warehouse.query.filter(Warehouse.code == code, Warehouse.id != (warehouse.id or 0)).first()
        if not code or not name:
            flash("Warehouse code and name are required.")
        elif clash:
            flash(f"Code {code} is already used by {clash.name}.")
        else:
            warehouse.code, warehouse.name = code, name
            warehouse.address = request.form.get("address", "").strip() or None
            warehouse.active = bool(request.form.get("active")) if warehouse_id else True
            db.session.add(warehouse)
            db.session.commit()
            flash(f"Saved {warehouse.name}.")
        return redirect(url_for("admin.warehouses"))
    return render_template("admin/warehouses.html", warehouses=Warehouse.query.order_by(Warehouse.name).all())


@bp.route("/inventory")
@admin_required
def inventory():
    """Products × warehouses grid of on-hand stock, with low stock flagged."""
    warehouses = Warehouse.query.filter_by(active=True).order_by(Warehouse.name).all()
    q = request.args.get("q", "").strip()
    query = Product.query.filter_by(active=True)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.name.ilike(like), Product.sku.ilike(like)))
    products = query.order_by(Product.category, Product.name).all()
    cells = {(i.product_id, i.warehouse_id): i for i in Inventory.query.all()}
    only_low = request.args.get("low") == "1"
    rows = []
    for p in products:
        row_cells = [cells.get((p.id, w.id)) for w in warehouses]
        low = any(c is not None and c.is_low for c in row_cells)
        if only_low and not low:
            continue
        rows.append({"product": p, "cells": row_cells, "total": sum(c.quantity for c in row_cells if c), "low": low})
    return render_template("admin/inventory.html", warehouses=warehouses, rows=rows, q=q, only_low=only_low)


@bp.route("/products/<int:product_id>/stock", methods=["POST"])
@admin_required
def stock_adjust(product_id):
    product = db.session.get(Product, product_id) or abort(404)
    note = request.form.get("note", "").strip()
    changed = 0
    for warehouse in Warehouse.query.all():
        qty = _int_or_none(request.form.get(f"qty_{warehouse.id}"))
        reorder = _int_or_none(request.form.get(f"reorder_{warehouse.id}"))
        if qty is None or qty < 0:
            continue
        inv = next((i for i in product.inventory if i.warehouse_id == warehouse.id), None)
        if inv and inv.quantity == qty and (reorder is None or inv.reorder_point == reorder):
            continue
        if not inv and qty == 0 and not reorder:
            continue
        fulfillment.adjust_stock(product.id, warehouse.id, qty, reorder if reorder is not None and reorder >= 0 else None,
                                 note, current_user().id)
        changed += 1
    db.session.commit()
    flash(f"Updated stock in {changed} warehouse{'s' if changed != 1 else ''}." if changed else "No stock changes.")
    return redirect(url_for("admin.product_edit", product_id=product.id) + "#stock")


@bp.route("/stock-history")
@admin_required
def stock_history():
    query = StockMovement.query
    if request.args.get("product_id"):
        query = query.filter_by(product_id=int(request.args["product_id"]))
    if request.args.get("warehouse_id"):
        query = query.filter_by(warehouse_id=int(request.args["warehouse_id"]))
    return render_template("admin/stock_history.html",
                           movements=query.order_by(StockMovement.created_at.desc()).limit(300).all())


# --- Orders -------------------------------------------------------------------

@bp.route("/orders")
@admin_required
def orders():
    status = request.args.get("status")
    query = Order.query
    if status in ORDER_STATUSES:
        query = query.filter_by(status=status)
    elif status == "open":
        query = query.filter(Order.status.in_(("submitted", "confirmed")))
    elif status == "unpaid":
        query = query.filter(Order.payment_method == "invoice", Order.paid.is_(False), Order.status != "cancelled")
    return render_template("admin/orders.html", orders=query.order_by(Order.created_at.desc()).limit(500).all(),
                           status=status, statuses=ORDER_STATUSES)


@bp.route("/orders/<int:order_id>", methods=["GET", "POST"])
@admin_required
def order_detail(order_id):
    order = db.session.get(Order, order_id) or abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        # Warehouse choices are saved with every action, so "Ship" uses
        # whatever is selected on screen right now.
        if order.status in ("submitted", "confirmed"):
            for item in order.items:
                wid = _int_or_none(request.form.get(f"warehouse_{item.id}"))
                if wid is not None:
                    item.warehouse_id = wid or None
        if action == "confirm" and order.status == "submitted":
            order.status = "confirmed"
            flash(f"{order.number} confirmed.")
        elif action == "ship":
            problems = fulfillment.ship(order, current_user().id)
            if problems:
                db.session.commit()  # keep the warehouse choices
                flash("Couldn't ship: " + " ".join(problems))
                return redirect(url_for("admin.order_detail", order_id=order.id))
            flash(f"{order.number} shipped — stock deducted.")
            mailer.send_shipped_notice(order)
        elif action == "deliver" and order.status == "shipped":
            order.status = "delivered"
            flash(f"{order.number} marked delivered.")
        elif action == "cancel":
            problems = fulfillment.cancel(order, current_user().id)
            flash(" ".join(problems) if problems else f"{order.number} cancelled.")
        elif action == "invoice":
            discount = _money_or_none(request.form.get("discount") or "0")
            tax = _money_or_none(request.form.get("tax") or "0")
            if discount is None or tax is None or discount > order.subtotal:
                flash("Discount and tax must be valid amounts, and the discount can't exceed the subtotal.")
            else:
                order.discount = discount
                if request.form.get("recalc_tax"):
                    order.recalculate_tax()
                else:
                    order.tax = tax
                order.invoice_message = request.form.get("invoice_message", "").strip() or None
                flash("Invoice updated.")
        elif action == "toggle_paid":
            order.paid = not order.paid
            flash(f"{order.number} marked {'paid' if order.paid else 'unpaid'}.")
        elif action == "save":
            flash("Warehouses saved.")
        db.session.commit()
        return redirect(url_for("admin.order_detail", order_id=order.id))

    open_order = order.status in ("submitted", "confirmed")
    stock = {item.id: {i.warehouse_id: i.quantity for i in item.product.inventory}
             for item in order.items if item.product}
    return render_template(
        "admin/order_detail.html", order=order, stock=stock, open_order=open_order,
        suggested=fulfillment.suggest_warehouses(order) if open_order else {},
        warehouses=Warehouse.query.filter_by(active=True).order_by(Warehouse.name).all(),
    )


@bp.route("/orders/<int:order_id>/invoice")
@admin_required
def invoice(order_id):
    return render_template("invoice.html", order=db.session.get(Order, order_id) or abort(404))


# --- Contact messages (leads) ------------------------------------------------

@bp.route("/messages")
@admin_required
def messages_list():
    return render_template("admin/messages.html",
                           messages=ContactMessage.query.order_by(ContactMessage.handled, ContactMessage.created_at.desc()).all())


@bp.route("/messages/<int:message_id>/handled", methods=["POST"])
@admin_required
def message_handled(message_id):
    msg = db.session.get(ContactMessage, message_id) or abort(404)
    msg.handled = not msg.handled
    db.session.commit()
    return redirect(url_for("admin.messages_list"))


# --- Spreadsheet import / export ------------------------------------------------

@bp.route("/import", methods=["GET", "POST"])
@admin_required
def import_page():
    if request.method == "POST":
        kind = request.form.get("kind")
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a CSV file to upload.")
            return redirect(url_for("admin.import_page"))
        if not upload.filename.lower().endswith(".csv"):
            flash("Upload a .csv file — in Excel or Google Sheets use File > Save As / Download > CSV.")
            return redirect(url_for("admin.import_page"))
        data = upload.read()
        try:
            if kind == "products":
                result = imports.import_products(data)
            elif kind == "prices":
                result = imports.import_prices(data, replace_customer_prices=bool(request.form.get("replace")))
            elif kind == "inventory":
                result = imports.import_inventory(data, user_id=current_user().id)
            else:
                abort(400)
        except ValueError as e:
            flash(f"Couldn't read that file: {e}")
            return redirect(url_for("admin.import_page"))
        return render_template("admin/import.html", result=result, kind=kind)
    return render_template("admin/import.html", result=None, kind=None)


@bp.route("/export/<kind>.csv")
@admin_required
def export(kind):
    if kind == "products":
        rows = [(p.sku, p.name, p.category or "", p.brand or "", p.unit or "", f"{p.base_price:.2f}",
                 p.description or "", p.image_url or "", "yes" if p.active else "no")
                for p in Product.query.order_by(Product.sku).all()]
        return _csv_response("products.csv", imports.export_csv(imports.PRODUCT_COLUMNS, rows))
    if kind == "prices":
        rows = [("", p.sku, 1, f"{p.base_price:.2f}") for p in Product.query.order_by(Product.sku).all()]
        for r in (Price.query.outerjoin(Customer).join(Product)
                  .order_by(Customer.name.nullsfirst(), Product.sku, Price.min_qty).all()):
            rows.append((r.customer.name if r.customer else "", r.product.sku, r.min_qty, f"{r.price:.2f}"))
        return _csv_response("prices.csv", imports.export_csv(imports.PRICE_COLUMNS, rows))
    if kind == "inventory":
        rows = [(i.warehouse.code, i.product.sku, i.quantity, i.reorder_point)
                for i in Inventory.query.join(Warehouse).join(Product).order_by(Warehouse.code, Product.sku).all()]
        return _csv_response("inventory.csv", imports.export_csv(imports.INVENTORY_COLUMNS, rows))
    abort(404)
