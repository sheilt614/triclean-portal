"""Spreadsheet (CSV) imports and exports for the admin backend.

TriClean's prices live in a spreadsheet today, so getting data in is a
"File > Save As > CSV" away. Each import is an upsert keyed on SKU (plus
customer / warehouse) — re-uploading an edited sheet updates rows in place
rather than duplicating them. Header names are matched case-insensitively
and ignore spaces/underscores, so "Base Price", "base_price", and
"BASEPRICE" all work.

Every import returns an ImportResult; nothing is committed if any row has
an error, so a half-applied sheet can't leave prices inconsistent.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from models import Customer, Inventory, Price, Product, StockMovement, Warehouse, db

PRODUCT_COLUMNS = ("sku", "name", "category", "brand", "unit", "base_price", "description", "image_url", "active")
PRICE_COLUMNS = ("customer", "sku", "min_qty", "price")
INVENTORY_COLUMNS = ("warehouse", "sku", "quantity", "reorder_point")


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _norm(header: str) -> str:
    return "".join(ch for ch in (header or "").lower() if ch.isalnum())


def _rows(file_bytes: bytes):
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("The file is empty.")
    # Map normalized header -> original, so lookups ignore case/spacing.
    headers = {_norm(h): h for h in reader.fieldnames if h}
    for line_no, row in enumerate(reader, start=2):  # line 1 is the header
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # blank line
        yield line_no, {key: (row.get(orig) or "").strip() for key, orig in headers.items()}


def _require_columns(file_bytes: bytes, required: tuple[str, ...]) -> None:
    first = file_bytes.decode("utf-8-sig", errors="replace").splitlines()[:1]
    present = {_norm(h) for h in next(csv.reader(first), [])} if first else set()
    missing = [c for c in required if _norm(c) not in present]
    if missing:
        raise ValueError(f"Missing column(s): {', '.join(missing)}. Expected headers: {', '.join(required)}.")


def _money(value: str, line_no: int, column: str, errors: list[str]) -> Decimal | None:
    cleaned = value.replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        errors.append(f"Line {line_no}: {column} '{value}' isn't a number.")
        return None
    if amount < 0:
        errors.append(f"Line {line_no}: {column} can't be negative.")
        return None
    return amount.quantize(Decimal("0.01"))


def _int(value: str, line_no: int, column: str, errors: list[str], default: int | None = None) -> int | None:
    if value == "" and default is not None:
        return default
    try:
        number = int(Decimal(value.replace(",", "")))
    except (InvalidOperation, ValueError):
        errors.append(f"Line {line_no}: {column} '{value}' isn't a whole number.")
        return None
    if number < 0:
        errors.append(f"Line {line_no}: {column} can't be negative.")
        return None
    return number


def _finish(result: ImportResult) -> ImportResult:
    if result.ok:
        db.session.commit()
    else:
        db.session.rollback()
        result.created = result.updated = result.deleted = 0
    return result


def import_products(file_bytes: bytes) -> ImportResult:
    """Columns: sku, name, base_price (required); category, brand, unit,
    description, image_url, active (optional — blank leaves the existing
    value alone on an update)."""
    _require_columns(file_bytes, ("sku", "name", "base_price"))
    result = ImportResult()
    seen = set()
    for line_no, row in _rows(file_bytes):
        sku = row.get("sku", "")
        if not sku:
            result.errors.append(f"Line {line_no}: missing SKU.")
            continue
        if sku.lower() in seen:
            result.errors.append(f"Line {line_no}: SKU {sku} appears twice in this file.")
            continue
        seen.add(sku.lower())
        price = _money(row.get("baseprice", ""), line_no, "base_price", result.errors)
        if price is None or not row.get("name"):
            if not row.get("name"):
                result.errors.append(f"Line {line_no}: missing name for SKU {sku}.")
            continue

        product = Product.query.filter(db.func.lower(Product.sku) == sku.lower()).first()
        if product:
            result.updated += 1
        else:
            product = Product(sku=sku)
            db.session.add(product)
            result.created += 1
        product.name = row["name"]
        product.base_price = price
        for column in ("category", "brand", "unit", "description"):
            if row.get(column):
                setattr(product, column, row[column])
        if row.get("imageurl"):
            product.image_url = row["imageurl"]
        if row.get("active"):
            product.active = row["active"].lower() not in ("no", "n", "false", "0", "inactive")
    return _finish(result)


def import_prices(file_bytes: bytes, replace_customer_prices: bool = False) -> ImportResult:
    """Columns: customer, sku, min_qty, price. Blank customer = the standard
    price list; blank min_qty = 1. A standard row at min_qty 1 sets the
    product's base price. With replace_customer_prices, every customer
    named in the file has their existing special prices cleared first, so
    the sheet becomes the complete list for them."""
    _require_columns(file_bytes, ("sku", "price"))
    result = ImportResult()
    customers = {c.name.strip().lower(): c for c in Customer.query.all()}
    products = {p.sku.lower(): p for p in Product.query.all()}
    parsed = []
    for line_no, row in _rows(file_bytes):
        name = row.get("customer", "")
        customer = None
        if name:
            customer = customers.get(name.lower())
            if not customer:
                result.errors.append(f"Line {line_no}: no customer named '{name}' — add them first, or check the spelling.")
                continue
        product = products.get(row.get("sku", "").lower())
        if not product:
            result.errors.append(f"Line {line_no}: no product with SKU '{row.get('sku', '')}'.")
            continue
        min_qty = _int(row.get("minqty", ""), line_no, "min_qty", result.errors, default=1)
        price = _money(row.get("price", ""), line_no, "price", result.errors)
        if min_qty is None or price is None:
            continue
        if min_qty < 1:
            result.errors.append(f"Line {line_no}: min_qty must be at least 1.")
            continue
        parsed.append((customer, product, min_qty, price))

    if result.errors:
        return _finish(result)

    if replace_customer_prices:
        for customer_id in {c.id for c, _, _, _ in parsed if c}:
            result.deleted += Price.query.filter_by(customer_id=customer_id).delete()

    for customer, product, min_qty, price in parsed:
        if customer is None and min_qty == 1:
            product.base_price = price
            result.updated += 1
            continue
        existing = Price.query.filter_by(
            product_id=product.id, customer_id=customer.id if customer else None, min_qty=min_qty
        ).first()
        if existing:
            existing.price = price
            result.updated += 1
        else:
            db.session.add(Price(product_id=product.id, customer_id=customer.id if customer else None,
                                 min_qty=min_qty, price=price))
            result.created += 1
        db.session.flush()  # so a later row for the same key in this file finds this one
    return _finish(result)


def import_inventory(file_bytes: bytes, user_id: int | None = None) -> ImportResult:
    """Columns: warehouse (its code or name), sku, quantity, reorder_point
    (optional). Sets the on-hand count to the sheet's number — a stock
    count, not an adjustment — and logs the difference as a movement."""
    _require_columns(file_bytes, ("warehouse", "sku", "quantity"))
    result = ImportResult()
    warehouses = {}
    for w in Warehouse.query.all():
        warehouses[w.code.lower()] = w
        warehouses[w.name.strip().lower()] = w
    products = {p.sku.lower(): p for p in Product.query.all()}
    for line_no, row in _rows(file_bytes):
        warehouse = warehouses.get(row.get("warehouse", "").lower())
        product = products.get(row.get("sku", "").lower())
        if not warehouse:
            result.errors.append(f"Line {line_no}: no warehouse '{row.get('warehouse', '')}'.")
            continue
        if not product:
            result.errors.append(f"Line {line_no}: no product with SKU '{row.get('sku', '')}'.")
            continue
        qty = _int(row.get("quantity", ""), line_no, "quantity", result.errors)
        reorder = _int(row.get("reorderpoint", ""), line_no, "reorder_point", result.errors, default=-1)
        if qty is None or reorder is None:
            continue
        inv = Inventory.query.filter_by(product_id=product.id, warehouse_id=warehouse.id).first()
        if inv:
            result.updated += 1
        else:
            inv = Inventory(product_id=product.id, warehouse_id=warehouse.id, quantity=0, reorder_point=0)
            db.session.add(inv)
            result.created += 1
        change = qty - inv.quantity
        inv.quantity = qty
        if reorder >= 0:
            inv.reorder_point = reorder
        if change:
            db.session.add(StockMovement(
                product_id=product.id, warehouse_id=warehouse.id, change=change, quantity_after=qty,
                reason="import", note="Spreadsheet stock count", user_id=user_id,
            ))
        db.session.flush()
    return _finish(result)


def export_csv(header: tuple[str, ...], rows) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()
