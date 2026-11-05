from __future__ import annotations

import os
import re
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from models import (  # noqa: E402
    AccountRequest, ContactMessage, Customer, Inventory, Order, Price, Product, StockMovement, User, Warehouse, db,
)
from pricing import PriceBook  # noqa: E402
import imports  # noqa: E402

D = Decimal


@pytest.fixture()
def app(tmp_path):
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}", "TESTING": True})
    with app.app_context():
        yield app
        db.session.remove()


@pytest.fixture()
def data(app):
    wh1, wh2 = Warehouse(code="BX1", name="Bronx 1"), Warehouse(code="QN1", name="Queens 1")
    xyz = Product(sku="XYZ", name="Product XYZ", category="Building Products", base_price=D("520.00"))
    soap = Product(sku="SOAP", name="Hand Soap", category="Janitorial", base_price=D("20.00"))
    a = Customer(name="Customer A")
    b = Customer(name="Customer B", discount_pct=D("10"))
    db.session.add_all([wh1, wh2, xyz, soap, a, b])
    db.session.flush()
    db.session.add_all([
        Price(product_id=xyz.id, customer_id=a.id, min_qty=1, price=D("500.00")),
        Price(product_id=xyz.id, customer_id=b.id, min_qty=1, price=D("450.00")),
        Price(product_id=xyz.id, customer_id=None, min_qty=10, price=D("480.00")),
        Price(product_id=soap.id, customer_id=None, min_qty=12, price=D("18.00")),
        Inventory(product_id=xyz.id, warehouse_id=wh1.id, quantity=5, reorder_point=2),
        Inventory(product_id=xyz.id, warehouse_id=wh2.id, quantity=20, reorder_point=2),
        Inventory(product_id=soap.id, warehouse_id=wh1.id, quantity=100, reorder_point=10),
    ])
    users = {}
    for email, role, cust in (("staff@t.test", "admin", None), ("a@t.test", "customer", a), ("b@t.test", "customer", b)):
        u = User(email=email, role=role, customer_id=cust.id if cust else None)
        u.set_password("password123")
        db.session.add(u)
        users[email] = u
    db.session.commit()
    return {"wh1": wh1, "wh2": wh2, "xyz": xyz, "soap": soap, "a": a, "b": b, **users}


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


def login(client, email):
    token = csrf(client)
    r = client.post("/login", data={"email": email, "password": "password123", "_csrf": token})
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    return csrf(client, "/portal/" if not email.startswith("staff") else "/admin/")


# --- Pricing ------------------------------------------------------------------

def test_each_customer_gets_their_own_price(data):
    assert PriceBook(data["a"]).unit_price(data["xyz"], 1) == D("500.00")
    assert PriceBook(data["b"]).unit_price(data["xyz"], 1) == D("450.00")
    assert PriceBook(None).unit_price(data["xyz"], 1) == D("520.00")


def test_customer_never_pays_more_than_standard_bulk_tier(data):
    # A's own price is $500, but the standard 10+ tier is $480 — they get $480 at 10.
    assert PriceBook(data["a"]).unit_price(data["xyz"], 10) == D("480.00")
    # B's discount (10%) applies to the standard tier: 480 * 0.9 = 432 < 450.
    assert PriceBook(data["b"]).unit_price(data["xyz"], 10) == D("432.00")


def test_discount_applies_where_customer_has_no_specific_price(data):
    assert PriceBook(data["b"]).unit_price(data["soap"], 1) == D("18.00")   # 20 * 0.9
    assert PriceBook(data["b"]).unit_price(data["soap"], 12) == D("16.20")  # 18 * 0.9


def test_tier_table_only_lists_breaks_that_lower_the_price(data):
    assert PriceBook(data["a"]).tiers(data["xyz"]) == [(1, D("500.00")), (10, D("480.00"))]
    assert PriceBook(data["a"]).tiers(data["soap"]) == [(1, D("20.00")), (12, D("18.00"))]


# --- Access control -------------------------------------------------------------

def test_customer_cannot_reach_backend_and_public_hides_prices(app, data):
    c = app.test_client()
    assert "$" not in c.get("/products").get_data(as_text=True).split("<main")[1].split("</main>")[0]
    login(c, "a@t.test")
    assert c.get("/admin/").status_code == 403
    assert c.get("/admin/customers/1/prices").status_code == 403


def test_catalog_shows_only_your_price(app, data):
    ca, cb = app.test_client(), app.test_client()
    login(ca, "a@t.test")
    login(cb, "b@t.test")
    page_a, page_b = ca.get("/portal/").get_data(as_text=True), cb.get("/portal/").get_data(as_text=True)
    assert "$500.00" in page_a and "$450.00" not in page_a
    assert "$450.00" in page_b and "$500.00" not in page_b


def test_csrf_required(app, data):
    c = app.test_client()
    r = c.post("/login", data={"email": "a@t.test", "password": "password123"})
    assert r.status_code == 400


def test_login_ignores_offsite_next(app, data):
    c = app.test_client()
    token = csrf(c)
    r = c.post("/login?next=//evil.example.com", data={"email": "a@t.test", "password": "password123", "_csrf": token})
    assert r.headers["Location"].endswith("/portal/")


# --- Ordering ---------------------------------------------------------------------

def place_order(client, token, product_id, qty, **extra):
    client.post("/portal/cart/add", data={"product_id": product_id, "qty": qty, "_csrf": token})
    return client.post("/portal/checkout", data={"shipping_address": "1 Test St", "_csrf": token, **extra})


def test_checkout_uses_server_side_price_and_bulk_tier(app, data):
    c = app.test_client()
    token = login(c, "a@t.test")
    r = place_order(c, token, data["xyz"].id, 10, unit_price="1.00")  # a tampered field is ignored
    assert r.status_code == 302
    order = Order.query.one()
    assert order.items[0].unit_price == D("480.00") and order.subtotal == D("4800.00")
    assert order.customer_id == data["a"].id and order.payment_method == "invoice"


def test_customer_cannot_see_another_customers_order(app, data):
    ca, cb = app.test_client(), app.test_client()
    place_order(ca, login(ca, "a@t.test"), data["soap"].id, 1)
    order = Order.query.one()
    login(cb, "b@t.test")
    assert cb.get(f"/portal/orders/{order.id}").status_code == 404
    assert cb.get(f"/portal/orders/{order.id}/invoice").status_code == 404
    assert ca.get(f"/portal/orders/{order.id}/invoice").status_code == 200


# --- Fulfillment ---------------------------------------------------------------------

def test_ship_deducts_from_chosen_warehouse_and_cancel_restores(app, data):
    c = app.test_client()
    place_order(c, login(c, "a@t.test"), data["xyz"].id, 8)
    order = Order.query.one()
    item = order.items[0]
    s = app.test_client()
    token = login(s, "staff@t.test")

    # BX1 only has 5 — shipping 8 from there must fail and change nothing.
    r = s.post(f"/admin/orders/{order.id}", data={"action": "ship", f"warehouse_{item.id}": data["wh1"].id, "_csrf": token})
    assert r.status_code == 302
    db.session.expire_all()
    assert Order.query.one().status == "submitted"
    assert Inventory.query.filter_by(warehouse_id=data["wh1"].id, product_id=data["xyz"].id).one().quantity == 5

    s.post(f"/admin/orders/{order.id}", data={"action": "ship", f"warehouse_{item.id}": data["wh2"].id, "_csrf": token})
    db.session.expire_all()
    assert Order.query.one().status == "shipped"
    assert Inventory.query.filter_by(warehouse_id=data["wh2"].id, product_id=data["xyz"].id).one().quantity == 12
    assert StockMovement.query.filter_by(reason="shipment").one().change == -8

    s.post(f"/admin/orders/{order.id}", data={"action": "cancel", "_csrf": token})
    db.session.expire_all()
    assert Order.query.one().status == "cancelled"
    assert Inventory.query.filter_by(warehouse_id=data["wh2"].id, product_id=data["xyz"].id).one().quantity == 20


def test_admin_customer_price_grid_sets_and_clears(app, data):
    s = app.test_client()
    token = login(s, "staff@t.test")
    s.post(f"/admin/customers/{data['a'].id}/prices",
           data={f"price_{data['soap'].id}": "15.50", f"price_{data['xyz'].id}": "", "_csrf": token})
    db.session.expire_all()
    assert Price.query.filter_by(customer_id=data["a"].id, product_id=data["soap"].id).one().price == D("15.50")
    assert Price.query.filter_by(customer_id=data["a"].id, product_id=data["xyz"].id, min_qty=1).first() is None


# --- Spreadsheet imports ---------------------------------------------------------------

def test_import_products_upserts(data):
    csv = b"SKU,Name,Base Price,Category\nSOAP,Hand Soap Refill,$21.50,Janitorial\nNEW1,Mop Bucket,\"1,035.00\",Janitorial\n"
    result = imports.import_products(csv)
    assert result.ok and result.created == 1 and result.updated == 1
    assert Product.query.filter_by(sku="NEW1").one().base_price == D("1035.00")
    assert Product.query.filter_by(sku="SOAP").one().name == "Hand Soap Refill"


def test_import_prices_all_or_nothing(data):
    bad = b"customer,sku,min_qty,price\nCustomer A,SOAP,1,15\nNobody Inc,SOAP,1,14\n"
    result = imports.import_prices(bad)
    assert not result.ok and "Nobody Inc" in result.errors[0]
    assert Price.query.filter_by(customer_id=data["a"].id, product_id=data["soap"].id).first() is None

    good = b"customer,sku,min_qty,price\nCustomer A,SOAP,1,15\nCustomer A,SOAP,24,13.75\n,XYZ,,515\n"
    result = imports.import_prices(good)
    assert result.ok, result.errors
    assert PriceBook(data["a"]).unit_price(data["soap"], 24) == D("13.75")
    assert Product.query.filter_by(sku="XYZ").one().base_price == D("515.00")


def test_import_inventory_sets_counts_and_logs(data):
    result = imports.import_inventory(b"warehouse,sku,quantity,reorder_point\nBX1,SOAP,80,15\nQueens 1,SOAP,30,\n")
    assert result.ok, result.errors
    assert Inventory.query.filter_by(warehouse_id=data["wh1"].id, product_id=data["soap"].id).one().quantity == 80
    assert Inventory.query.filter_by(warehouse_id=data["wh2"].id, product_id=data["soap"].id).one().quantity == 30
    assert {m.change for m in StockMovement.query.filter_by(reason="import")} == {-20, 30}


# --- Accounts -------------------------------------------------------------------------

def test_invite_link_sets_password_once(app, data):
    s = app.test_client()
    token = login(s, "staff@t.test")
    r = s.post(f"/admin/customers/{data['a'].id}/users", data={"email": "new@t.test", "name": "New", "_csrf": token},
               follow_redirects=True)
    link = re.search(r"/set-password/([\w-]+)", r.get_data(as_text=True)).group(0)
    c = app.test_client()
    t = csrf(c, link)
    r = c.post(link, data={"password": "longenough1", "confirm": "longenough1", "_csrf": t})
    assert r.headers["Location"].endswith("/portal/")
    assert app.test_client().get(link).status_code == 404  # used up
    assert User.query.filter_by(email="new@t.test").one().check_password("longenough1")


def test_open_account_request_saved(app, data):
    c = app.test_client()
    token = csrf(c, "/open-an-account")
    c.post("/open-an-account", data={"business_name": "Joe's Bar", "email": "joe@bar.test", "_csrf": token})
    assert AccountRequest.query.one().business_name == "Joe's Bar"


def test_every_admin_page_renders(app, data):
    c = app.test_client()
    place_order(c, login(c, "a@t.test"), data["soap"].id, 2)
    s = app.test_client()
    login(s, "staff@t.test")
    order = Order.query.one()
    for path in ["/admin/", "/admin/products", f"/admin/products/{data['xyz'].id}", "/admin/products/new",
                 "/admin/customers", f"/admin/customers/{data['a'].id}", "/admin/customers/new",
                 f"/admin/customers/{data['b'].id}/prices", "/admin/warehouses", "/admin/inventory",
                 "/admin/inventory?low=1", "/admin/stock-history", "/admin/orders?status=open",
                 f"/admin/orders/{order.id}", f"/admin/orders/{order.id}/invoice", "/admin/requests",
                 "/admin/import", "/admin/staff", "/admin/messages", "/admin/export/products.csv", "/admin/export/prices.csv",
                 "/admin/export/inventory.csv"]:
        assert s.get(path).status_code == 200, path
    for path in ["/portal/", "/portal/cart", "/portal/checkout", "/portal/orders", f"/portal/orders/{order.id}",
                 "/portal/account"]:
        assert c.get(path).status_code in (200, 302), path
    for path in ["/", "/products", "/open-an-account", "/login"]:
        assert app.test_client().get(path).status_code == 200, path


def test_invoice_tax_discount_and_balance(app, data):
    data["a"].tax_rate = D("8.875")
    db.session.commit()
    c = app.test_client()
    place_order(c, login(c, "a@t.test"), data["soap"].id, 10)  # 10 x $20 = $200
    order = Order.query.one()
    assert order.tax == D("17.75") and order.total == D("217.75")

    s = app.test_client()
    token = login(s, "staff@t.test")
    s.post(f"/admin/orders/{order.id}", data={"action": "invoice", "discount": "20", "tax": "0",
                                              "recalc_tax": "1", "invoice_message": "Thanks!", "_csrf": token})
    db.session.expire_all()
    order = Order.query.one()
    assert order.discount == D("20.00") and order.tax == D("15.98")  # (200 - 20) * 8.875%
    assert order.total == D("195.98") and order.balance_due == D("195.98")
    invoice = s.get(f"/admin/orders/{order.id}/invoice").get_data(as_text=True)
    assert "$195.98" in invoice and "Thanks!" in invoice and "Net 30" in invoice
    s.post(f"/admin/orders/{order.id}", data={"action": "toggle_paid", "_csrf": token})
    db.session.expire_all()
    assert Order.query.one().balance_due == D("0.00")


def test_contact_message_reaches_backend_and_no_phone_on_public_pages(app, data):
    c = app.test_client()
    for path in ["/", "/products", "/open-an-account", "/login", "/contact"]:
        assert "582-9876" not in c.get(path).get_data(as_text=True), path
    token = csrf(c, "/contact")
    r = c.post("/contact", data={"name": "Pat Facilities", "email": "pat@hospital.test",
                                 "message": "Need a quote for 12 buildings.", "_csrf": token})
    assert "Thanks, Pat." in r.get_data(as_text=True)
    s = app.test_client()
    login(s, "staff@t.test")
    page = s.get("/admin/messages").get_data(as_text=True)
    assert "Need a quote for 12 buildings." in page and "pat@hospital.test" in page


def test_industry_dropdown_and_other(app, data):
    c = app.test_client()
    page = c.get("/contact").get_data(as_text=True)
    assert "Hospital / healthcare" in page and 'value="Other"' in page
    token = csrf(c, "/contact")
    c.post("/contact", data={"name": "Sam", "email": "s@x.test", "message": "Hi", "business_type": "Other",
                             "business_type_other": "Stadium", "_csrf": token})
    c.post("/contact", data={"name": "Ann", "email": "a@x.test", "message": "Hi", "business_type": "Hotel",
                             "_csrf": token})
    c.post("/contact", data={"name": "Bob", "email": "b@x.test", "message": "Hi", "business_type": "<script>",
                             "_csrf": token})
    assert [m.industry for m in ContactMessage.query.order_by(ContactMessage.id)] == ["Other: Stadium", "Hotel", None]
    token = csrf(c, "/open-an-account")
    c.post("/open-an-account", data={"business_name": "Big U", "email": "u@x.test", "business_type": "Other",
                                     "business_type_other": "Airport", "_csrf": token})
    assert AccountRequest.query.one().business_type == "Other: Airport"


def test_missing_column_added_to_existing_table(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE contact_message (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
                "organization VARCHAR(255), email VARCHAR(255) NOT NULL, phone VARCHAR(50), "
                "message TEXT NOT NULL, handled BOOLEAN NOT NULL, created_at DATETIME)")
    con.commit(); con.close()
    create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}"})
    cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(contact_message)")]
    assert "industry" in cols
