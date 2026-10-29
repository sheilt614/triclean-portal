"""Order status changes that touch stock. Shipping an order takes each line's
quantity out of the warehouse chosen for it; cancelling a shipped order
puts it back. Both are all-or-nothing and logged as StockMovements."""
from __future__ import annotations

from datetime import datetime

from models import Inventory, Order, StockMovement, db


def suggest_warehouses(order: Order) -> dict[int, int]:
    """For each line without a warehouse yet: {item id: warehouse id} of the
    active warehouse holding the most stock of that product — a default
    staff can change. Doesn't modify the order."""
    suggestions = {}
    for item in order.items:
        if item.warehouse_id or not item.product:
            continue
        best = max((i for i in item.product.inventory if i.warehouse.active),
                   key=lambda i: i.quantity, default=None)
        if best and best.quantity > 0:
            suggestions[item.id] = best.warehouse_id
    return suggestions


def ship(order: Order, user_id: int | None) -> list[str]:
    """Marks the order shipped and deducts stock. Returns a list of problems
    (and changes nothing) if any line has no warehouse or not enough stock."""
    if order.status not in ("submitted", "confirmed"):
        return [f"Only submitted or confirmed orders can be shipped (this one is {order.status})."]
    problems = []
    rows = []
    # Several lines can draw on the same product+warehouse; check the total.
    needed: dict[tuple[int, int], int] = {}
    for item in order.items:
        if not item.warehouse_id:
            problems.append(f"{item.sku}: choose which warehouse it ships from.")
            continue
        if not item.product_id:
            problems.append(f"{item.sku}: product no longer exists, so stock can't be deducted.")
            continue
        key = (item.product_id, item.warehouse_id)
        needed[key] = needed.get(key, 0) + item.quantity
        rows.append(item)
    for (product_id, warehouse_id), qty in needed.items():
        inv = Inventory.query.filter_by(product_id=product_id, warehouse_id=warehouse_id).first()
        have = inv.quantity if inv else 0
        if have < qty:
            item = next(i for i in rows if (i.product_id, i.warehouse_id) == (product_id, warehouse_id))
            problems.append(f"{item.sku}: needs {qty} from {item.warehouse.name}, only {have} on hand.")
    if problems:
        return problems

    for item in rows:
        inv = Inventory.query.filter_by(product_id=item.product_id, warehouse_id=item.warehouse_id).first()
        inv.quantity -= item.quantity
        db.session.add(StockMovement(
            product_id=item.product_id, warehouse_id=item.warehouse_id, change=-item.quantity,
            quantity_after=inv.quantity, reason="shipment", note=order.number,
            order_id=order.id, user_id=user_id,
        ))
    order.status = "shipped"
    order.shipped_at = datetime.utcnow()
    db.session.commit()
    return []


def cancel(order: Order, user_id: int | None) -> list[str]:
    """Cancels the order; if it had already shipped, returns its stock to
    the warehouses it came from."""
    if order.status in ("cancelled", "delivered"):
        return [f"A {order.status} order can't be cancelled."]
    if order.status == "shipped":
        for item in order.items:
            if not (item.product_id and item.warehouse_id):
                continue
            inv = Inventory.query.filter_by(product_id=item.product_id, warehouse_id=item.warehouse_id).first()
            if not inv:
                inv = Inventory(product_id=item.product_id, warehouse_id=item.warehouse_id, quantity=0)
                db.session.add(inv)
            inv.quantity += item.quantity
            db.session.add(StockMovement(
                product_id=item.product_id, warehouse_id=item.warehouse_id, change=item.quantity,
                quantity_after=inv.quantity, reason="cancellation", note=order.number,
                order_id=order.id, user_id=user_id,
            ))
    order.status = "cancelled"
    db.session.commit()
    return []


def adjust_stock(product_id: int, warehouse_id: int, new_quantity: int, reorder_point: int | None,
                 note: str | None, user_id: int | None) -> Inventory:
    """Manual stock correction from the backend (a recount, damage, a delivery
    received). Sets the absolute quantity and logs the difference."""
    inv = Inventory.query.filter_by(product_id=product_id, warehouse_id=warehouse_id).first()
    if not inv:
        inv = Inventory(product_id=product_id, warehouse_id=warehouse_id, quantity=0, reorder_point=0)
        db.session.add(inv)
    change = new_quantity - (inv.quantity or 0)
    inv.quantity = new_quantity
    if reorder_point is not None:
        inv.reorder_point = reorder_point
    if change:
        db.session.add(StockMovement(
            product_id=product_id, warehouse_id=warehouse_id, change=change,
            quantity_after=new_quantity, reason="adjustment", note=note or None, user_id=user_id,
        ))
    return inv
