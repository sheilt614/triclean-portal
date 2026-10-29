"""Customer portal: a logged-in customer's catalog at their own prices,
cart, ordering on account, and order history."""
from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from auth import current_user, customer_required
from models import Order, OrderItem, Product, db
from pricing import PriceBook

bp = Blueprint("portal", __name__, url_prefix="/portal")

MAX_QTY = 100000


def _cart() -> dict[str, int]:
    # Kept in the (signed) session cookie: {product_id: qty}. A cart of a
    # few dozen lines is well under the cookie size limit.
    return session.setdefault("cart", {})


def _save_cart(cart: dict[str, int]) -> None:
    session["cart"] = {k: v for k, v in cart.items() if v > 0}
    session.modified = True


def _parse_qty(value) -> int:
    try:
        return max(0, min(int(value), MAX_QTY))
    except (TypeError, ValueError):
        return 0


def _cart_lines(customer):
    """Cart contents priced for this customer right now. Products that were
    deactivated since being added are dropped silently."""
    cart = _cart()
    ids = [int(i) for i in cart]
    products = Product.query.filter(Product.id.in_(ids), Product.active.is_(True)).all() if ids else []
    book = PriceBook(customer, products)
    lines = []
    for product in sorted(products, key=lambda p: p.name):
        qty = cart[str(product.id)]
        unit = book.unit_price(product, qty)
        lines.append({"product": product, "qty": qty, "unit_price": unit,
                      "line_total": unit * qty, "tiers": book.tiers(product)})
    return lines


@bp.app_context_processor
def inject_cart_count():
    user = current_user()
    if user and not user.is_admin:
        return {"cart_count": sum(session.get("cart", {}).values())}
    return {"cart_count": 0}


@bp.route("/")
@customer_required
def catalog():
    customer = current_user().customer
    q = request.args.get("q", "").strip()
    category = request.args.get("category") or None
    query = Product.query.filter_by(active=True)
    if category:
        query = query.filter_by(category=category)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.name.ilike(like), Product.sku.ilike(like), Product.brand.ilike(like)))
    products = query.order_by(Product.category, Product.name).all()
    book = PriceBook(customer, products)
    categories = sorted({c for (c,) in db.session.query(Product.category).filter_by(active=True).distinct() if c})
    rows = [{"product": p, "tiers": book.tiers(p), "special": book.has_special_price(p)} for p in products]
    return render_template("portal/catalog.html", rows=rows, categories=categories,
                           category=category, q=q, cart=_cart())


@bp.route("/cart/add", methods=["POST"])
@customer_required
def cart_add():
    product = Product.query.filter_by(id=request.form.get("product_id", type=int), active=True).first_or_404()
    qty = _parse_qty(request.form.get("qty", 1))
    if qty:
        cart = _cart()
        cart[str(product.id)] = min(cart.get(str(product.id), 0) + qty, MAX_QTY)
        _save_cart(cart)
        flash(f"Added {qty} × {product.name} to your order.")
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/portal") else url_for("portal.catalog"))


@bp.route("/cart", methods=["GET", "POST"])
@customer_required
def cart():
    if request.method == "POST":
        cart = _cart()
        for key in list(cart):
            if f"qty_{key}" in request.form:
                cart[key] = _parse_qty(request.form.get(f"qty_{key}"))
        _save_cart(cart)
        if request.form.get("action") == "checkout":
            return redirect(url_for("portal.checkout"))
        flash("Order updated.")
        return redirect(url_for("portal.cart"))
    lines = _cart_lines(current_user().customer)
    return render_template("portal/cart.html", lines=lines, subtotal=sum((l["line_total"] for l in lines), Decimal(0)))


@bp.route("/checkout", methods=["GET", "POST"])
@customer_required
def checkout():
    user = current_user()
    customer = user.customer
    lines = _cart_lines(customer)
    if not lines:
        flash("Your order is empty.")
        return redirect(url_for("portal.catalog"))
    subtotal = sum((l["line_total"] for l in lines), Decimal(0))

    if request.method == "POST":
        shipping = request.form.get("shipping_address", "").strip()
        if not shipping:
            flash("Please enter a delivery address.")
        else:
            # Prices are recomputed above at submit time, never taken from the form.
            order = Order(
                customer_id=customer.id, placed_by_id=user.id,
                payment_method=customer.payment_terms,
                po_number=request.form.get("po_number", "").strip() or None,
                notes=request.form.get("notes", "").strip() or None,
                shipping_address=shipping, subtotal=subtotal,
            )
            for line in lines:
                p = line["product"]
                order.items.append(OrderItem(
                    product_id=p.id, sku=p.sku, name=p.name, unit=p.unit,
                    quantity=line["qty"], unit_price=line["unit_price"],
                ))
            db.session.add(order)
            db.session.commit()
            _save_cart({})
            flash(f"Order {order.number} placed. TriClean will confirm it shortly.")
            return redirect(url_for("portal.order_detail", order_id=order.id))

    return render_template("portal/checkout.html", lines=lines, subtotal=subtotal, customer=customer)


@bp.route("/orders")
@customer_required
def orders():
    return render_template("portal/orders.html", orders=current_user().customer.orders)


def _own_order(order_id: int) -> Order:
    order = db.session.get(Order, order_id)
    if not order or order.customer_id != current_user().customer_id:
        abort(404)
    return order


@bp.route("/orders/<int:order_id>")
@customer_required
def order_detail(order_id):
    return render_template("portal/order_detail.html", order=_own_order(order_id))


@bp.route("/orders/<int:order_id>/invoice")
@customer_required
def invoice(order_id):
    return render_template("invoice.html", order=_own_order(order_id))


@bp.route("/orders/<int:order_id>/reorder", methods=["POST"])
@customer_required
def reorder(order_id):
    """Puts every still-available item from a past order back in the cart,
    at today's prices (shown in the cart before anything is placed)."""
    order = _own_order(order_id)
    cart = _cart()
    added = 0
    for item in order.items:
        if item.product and item.product.active:
            cart[str(item.product_id)] = min(cart.get(str(item.product_id), 0) + item.quantity, MAX_QTY)
            added += 1
    _save_cart(cart)
    missing = len(order.items) - added
    flash(f"Added {added} item{'s' if added != 1 else ''} from {order.number} to your order"
          + (f" ({missing} no longer available)." if missing else "."))
    return redirect(url_for("portal.cart"))


@bp.route("/account")
@customer_required
def account():
    return render_template("portal/account.html", customer=current_user().customer)
