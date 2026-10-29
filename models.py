"""Database models for the TriClean Supply ordering portal.

The shape of the business: TriClean sells to business customers (bars,
restaurants, property managers...) on account. Each customer company can
have several logins, its own prices, and its own bulk tiers; stock is held
across several warehouses, and each order line ships from one of them.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from decimal import Decimal

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

Money = db.Numeric(10, 2)


class Customer(db.Model):
    """A business that buys from TriClean — the unit prices and orders
    belong to. People who log in on its behalf are Users."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    contact_name = db.Column(db.String(255))
    email = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    billing_address = db.Column(db.Text)
    shipping_address = db.Column(db.Text)
    # "invoice" (billed on account, the default) or "card" (pay at order —
    # recorded now, actual card processing comes later).
    payment_terms = db.Column(db.String(20), default="invoice", nullable=False)
    net_days = db.Column(db.Integer, default=30, nullable=False)
    # Applied to the standard price list for any product this customer has
    # no specific price for — "5% off everything" without 50 override rows.
    discount_pct = db.Column(db.Numeric(5, 2), default=0, nullable=False)
    # Sales tax charged on this customer's orders, e.g. 8.875 for NYC. 0 for
    # customers who buy for resale or are otherwise exempt.
    tax_rate = db.Column(db.Numeric(6, 3), default=0, nullable=False)
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    users = db.relationship("User", backref="customer", lazy=True)
    orders = db.relationship("Order", backref="customer", lazy=True, order_by="Order.created_at.desc()")


class User(db.Model):
    """A login. role="admin" is TriClean staff (the backend); role="customer"
    belongs to one Customer and only ever sees that customer's prices and
    orders. Accounts are created by an admin and activated through a
    one-time invite link where the person picks their own password."""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255))
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(20), default="customer", nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    active = db.Column(db.Boolean, default=True, nullable=False)
    invite_token = db.Column(db.String(64), unique=True, index=True)
    invite_expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login_at = db.Column(db.DateTime)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def set_password(self, password: str) -> None:
        # PBKDF2 rather than werkzeug's scrypt default: scrypt is missing from
        # some Python builds (e.g. macOS's LibreSSL one), PBKDF2 works everywhere.
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256:600000")

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    def new_invite(self, days: int = 7) -> str:
        """(Re)issues a one-time link for setting a password — used both for
        first-time invites and for "forgot password" resets by an admin."""
        self.invite_token = secrets.token_urlsafe(32)
        self.invite_expires_at = datetime.utcnow() + timedelta(days=days)
        return self.invite_token


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(100), index=True)
    brand = db.Column(db.String(100))
    unit = db.Column(db.String(100))  # what one quantity is, e.g. "case of 4 x 1 gal"
    description = db.Column(db.Text)
    image_url = db.Column(db.String(500))
    # The standard price for one unit — what any customer without a special
    # price pays at quantity 1. Bulk tiers on top of it live in Price.
    base_price = db.Column(Money, nullable=False, default=0)
    active = db.Column(db.Boolean, default=True, nullable=False)

    inventory = db.relationship("Inventory", backref="product", lazy=True, cascade="all, delete-orphan")
    prices = db.relationship("Price", backref="product", lazy=True, cascade="all, delete-orphan")

    @property
    def total_stock(self) -> int:
        return sum(i.quantity for i in self.inventory)


class Price(db.Model):
    """One price break. customer_id NULL = the standard price list everyone
    gets; set = that customer's own price. min_qty is the quantity from
    which the price applies ("10+ for $450"). See pricing.py for how a
    customer's price is picked from these."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), index=True)
    min_qty = db.Column(db.Integer, nullable=False, default=1)
    price = db.Column(Money, nullable=False)

    customer = db.relationship("Customer")

    __table_args__ = (db.UniqueConstraint("product_id", "customer_id", "min_qty"),)


class Warehouse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)  # short id used in spreadsheets, e.g. "BX1"
    name = db.Column(db.String(255), nullable=False)
    address = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True, nullable=False)

    inventory = db.relationship("Inventory", backref="warehouse", lazy=True)


class Inventory(db.Model):
    """How many of one product are on hand in one warehouse."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouse.id"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    # Below this, the product shows as low stock in this warehouse.
    reorder_point = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (db.UniqueConstraint("product_id", "warehouse_id"),)

    @property
    def is_low(self) -> bool:
        return self.quantity <= self.reorder_point


class StockMovement(db.Model):
    """Every change to an Inventory quantity, with why — shipments, manual
    adjustments, spreadsheet imports — so "where did the stock go?" always
    has an answer."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouse.id"), nullable=False)
    change = db.Column(db.Integer, nullable=False)
    quantity_after = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(50), nullable=False)  # shipment | adjustment | import | cancellation
    note = db.Column(db.String(255))
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    product = db.relationship("Product")
    warehouse = db.relationship("Warehouse")
    user = db.relationship("User")


ORDER_STATUSES = ("submitted", "confirmed", "shipped", "delivered", "cancelled")


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False, index=True)
    placed_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    status = db.Column(db.String(20), default="submitted", nullable=False, index=True)
    payment_method = db.Column(db.String(20), default="invoice", nullable=False)
    paid = db.Column(db.Boolean, default=False, nullable=False)
    po_number = db.Column(db.String(100))  # the customer's own purchase-order reference
    notes = db.Column(db.Text)
    shipping_address = db.Column(db.Text)
    subtotal = db.Column(Money, nullable=False, default=0)
    # Invoice adjustments, matching TriClean's invoice template: a discount
    # off the subtotal and tax (worked out from the customer's tax rate at
    # order time; staff can override both), plus a message printed on it.
    discount = db.Column(Money, nullable=False, default=0)
    tax = db.Column(Money, nullable=False, default=0)
    invoice_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    shipped_at = db.Column(db.DateTime)

    placed_by = db.relationship("User")
    items = db.relationship("OrderItem", backref="order", lazy=True, cascade="all, delete-orphan")

    @property
    def number(self) -> str:
        return f"TC-{self.id:05d}"

    @property
    def total(self) -> Decimal:
        return (self.subtotal - (self.discount or 0) + (self.tax or 0)).quantize(Decimal("0.01"))

    @property
    def balance_due(self) -> Decimal:
        return Decimal("0.00") if self.paid else self.total

    @property
    def terms_label(self) -> str:
        return f"Net {self.customer.net_days}" if self.payment_method == "invoice" else "Card"

    def recalculate_tax(self) -> None:
        rate = Decimal(self.customer.tax_rate or 0)
        self.tax = ((self.subtotal - (self.discount or 0)) * rate / 100).quantize(Decimal("0.01"))

    @property
    def invoice_due(self) -> datetime | None:
        if self.payment_method != "invoice":
            return None
        return (self.shipped_at or self.created_at) + timedelta(days=self.customer.net_days)


class OrderItem(db.Model):
    """A line on an order. SKU, name, and price are copied in at order time
    so later catalog or price changes never rewrite an order's history."""
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    sku = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    unit = db.Column(db.String(100))
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(Money, nullable=False)
    # Which warehouse this line ships from — chosen by staff when
    # fulfilling, required before the order can be marked shipped.
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouse.id"))

    product = db.relationship("Product")
    warehouse = db.relationship("Warehouse")

    @property
    def line_total(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))


class AccountRequest(db.Model):
    """Submitted from the public "Open an account" form — a lead for staff
    to follow up on and, if approved, turn into a Customer."""
    id = db.Column(db.Integer, primary_key=True)
    business_name = db.Column(db.String(255), nullable=False)
    contact_name = db.Column(db.String(255))
    email = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50))
    business_type = db.Column(db.String(100))
    message = db.Column(db.Text)
    handled = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
