"""Fills an EMPTY local database with demo data, to click around before real
data is imported. Refuses to run if any products exist, so it can never
mix demo rows into real data.

    python seed.py

Demo logins (local only):
    admin@demo.test     / demo-admin-pass
    buyer@barandgrill.test  / demo-buyer-pass   (Demo Bar & Grill)
    buyer@propmgmt.test     / demo-buyer-pass   (Demo Property Management)
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

from app import create_app
from models import Customer, Inventory, Price, Product, User, Warehouse, db

PRODUCTS = [
    # sku, name, category, brand, unit, base price
    ("TC-DG-1G", "Heavy-Duty Kitchen Degreaser", "Chemicals", "TriClean", "case of 4 x 1 gal", "64.00"),
    ("TC-SAN-1G", "Quaternary Sanitizer (no-rinse)", "Chemicals", "TriClean", "case of 4 x 1 gal", "58.00"),
    ("TC-GLS-32", "Glass & Bar-Top Cleaner", "Chemicals", "TriClean", "case of 12 x 32 oz", "42.00"),
    ("TC-FLR-5G", "Neutral Floor Cleaner", "Chemicals", "TriClean", "5 gal pail", "48.00"),
    ("PP-TT-2PLY", "2-Ply Toilet Tissue", "Paper", "Generic", "case of 96 rolls", "71.00"),
    ("PP-HT-ROLL", "Hardwound Roll Towel 800'", "Paper", "Generic", "case of 6 rolls", "46.00"),
    ("PP-NAP-DIN", "Dinner Napkins, white", "Food Service", "Generic", "case of 3,000", "52.00"),
    ("FS-GLV-NIT-L", "Nitrile Gloves, Large", "Food Service", "Generic", "case of 1,000", "89.00"),
    ("JN-LNR-4045", "Can Liners 40x46, 1.5 mil", "Janitorial", "Generic", "case of 100", "38.00"),
    ("JN-MOP-24", "Wet Mop Head, 24 oz", "Janitorial", "Generic", "each", "9.50"),
    ("AS-6045013", "American Standard Flowise Manual Urinal Flush Valve 0.125 gpf", "Plumbing",
     "American Standard", "each", "265.00"),
    ("BP-XYZ-500", "Commercial Floor Machine Pad Kit", "Building Products", "Generic", "kit", "520.00"),
]

WAREHOUSES = [("BX1", "Bronx — Netherland Ave"), ("BX2", "Bronx — Hunts Point"),
              ("QN1", "Queens — Long Island City"), ("BK1", "Brooklyn — Red Hook")]


def main() -> None:
    app = create_app()
    with app.app_context():
        if Product.query.first():
            sys.exit("Products already exist — seed.py only runs on an empty database.")
        if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite") and "--force" not in sys.argv:
            sys.exit("Refusing to seed a non-SQLite database without --force.")

        warehouses = [Warehouse(code=c, name=n) for c, n in WAREHOUSES]
        db.session.add_all(warehouses)
        products = []
        for sku, name, cat, brand, unit, price in PRODUCTS:
            p = Product(sku=sku, name=name, category=cat, brand=brand, unit=unit, base_price=Decimal(price))
            products.append(p)
        db.session.add_all(products)
        db.session.flush()

        for i, p in enumerate(products):
            for j, w in enumerate(warehouses):
                qty = (i * 7 + j * 13) % 60
                db.session.add(Inventory(product_id=p.id, warehouse_id=w.id, quantity=qty, reorder_point=10))
            db.session.add(Price(product_id=p.id, min_qty=10, price=(p.base_price * Decimal("0.92")).quantize(Decimal("0.01"))))

        bar = Customer(name="Demo Bar & Grill", contact_name="Sam Demo", email="buyer@barandgrill.test",
                       shipping_address="100 Demo St\nBronx, NY 10463", billing_address="100 Demo St\nBronx, NY 10463", tax_rate=Decimal("8.875"))
        pm = Customer(name="Demo Property Management", contact_name="Pat Demo", email="buyer@propmgmt.test",
                      shipping_address="200 Sample Ave\nYonkers, NY 10701", discount_pct=Decimal("5"), net_days=45)
        db.session.add_all([bar, pm])
        db.session.flush()

        # The owner's example: the same product at $500 for one customer and $450 for another.
        xyz = next(p for p in products if p.sku == "BP-XYZ-500")
        db.session.add(Price(product_id=xyz.id, customer_id=bar.id, min_qty=1, price=Decimal("500.00")))
        db.session.add(Price(product_id=xyz.id, customer_id=pm.id, min_qty=1, price=Decimal("450.00")))
        db.session.add(Price(product_id=xyz.id, customer_id=pm.id, min_qty=5, price=Decimal("425.00")))
        degreaser = next(p for p in products if p.sku == "TC-DG-1G")
        db.session.add(Price(product_id=degreaser.id, customer_id=bar.id, min_qty=1, price=Decimal("55.00")))

        admin = User(email="admin@demo.test", name="Demo Admin", role="admin")
        admin.set_password("demo-admin-pass")
        b1 = User(email="buyer@barandgrill.test", name="Sam Demo", customer_id=bar.id)
        b1.set_password("demo-buyer-pass")
        b2 = User(email="buyer@propmgmt.test", name="Pat Demo", customer_id=pm.id)
        b2.set_password("demo-buyer-pass")
        db.session.add_all([admin, b1, b2])
        db.session.commit()
        print(f"Seeded {len(products)} products, {len(warehouses)} warehouses, 2 customers. "
              f"Database: {app.config['SQLALCHEMY_DATABASE_URI']}")


if __name__ == "__main__":
    os.environ.setdefault("FLASK_ENV", "development")
    main()
