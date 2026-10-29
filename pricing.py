"""How a customer's price for a product is worked out.

Two price lists can apply to any customer:

* The standard list: the product's base price at quantity 1, plus any
  standard bulk tiers (Price rows with no customer), minus the customer's
  blanket discount_pct if they have one.
* The customer's own list: Price rows for that customer specifically —
  their own unit price and/or their own bulk tiers.

For a given quantity, each list's applicable tier is the one with the
highest min_qty at or below that quantity, and the customer pays the LOWER
of the two. So a special price never leaves a customer paying more than
the standard bulk price would have been, and the owner controls everything
by setting either list.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from models import Customer, Price, Product

CENT = Decimal("0.01")


def _money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _tier_for(breaks: list[tuple[int, Decimal]], qty: int) -> Decimal | None:
    applicable = [price for min_qty, price in breaks if min_qty <= qty]
    return applicable[-1] if applicable else None


class PriceBook:
    """All price breaks relevant to one customer, loaded in two queries so a
    full catalog page doesn't run a query per product."""

    def __init__(self, customer: Customer | None, products: list[Product] | None = None):
        self.customer = customer
        query = Price.query
        if products is not None:
            query = query.filter(Price.product_id.in_([p.id for p in products]))
        standard: dict[int, list[tuple[int, Decimal]]] = {}
        own: dict[int, list[tuple[int, Decimal]]] = {}
        for row in query.filter(Price.customer_id.is_(None)).all():
            standard.setdefault(row.product_id, []).append((row.min_qty, row.price))
        if customer is not None:
            for row in query.filter(Price.customer_id == customer.id).all():
                own.setdefault(row.product_id, []).append((row.min_qty, row.price))
        self._standard = standard
        self._own = own
        self._discount = Decimal(customer.discount_pct or 0) if customer else Decimal(0)

    def standard_breaks(self, product: Product) -> list[tuple[int, Decimal]]:
        breaks = {1: Decimal(product.base_price)}
        for min_qty, price in self._standard.get(product.id, []):
            breaks[min_qty] = Decimal(price)
        factor = (Decimal(100) - self._discount) / Decimal(100)
        return sorted((q, _money(p * factor)) for q, p in breaks.items())

    def own_breaks(self, product: Product) -> list[tuple[int, Decimal]]:
        return sorted((q, _money(p)) for q, p in self._own.get(product.id, []))

    def has_special_price(self, product: Product) -> bool:
        return bool(self._own.get(product.id)) or self._discount > 0

    def unit_price(self, product: Product, qty: int = 1) -> Decimal:
        qty = max(qty, 1)
        candidates = [
            p for p in (_tier_for(self.standard_breaks(product), qty),
                        _tier_for(self.own_breaks(product), qty))
            if p is not None
        ]
        return min(candidates)

    def tiers(self, product: Product) -> list[tuple[int, Decimal]]:
        """The customer's effective price at every quantity where it can
        change — what the catalog shows as "1+: $500 · 10+: $450". Breaks
        that don't actually lower the price are dropped."""
        points = sorted({q for q, _ in self.standard_breaks(product)} | {q for q, _ in self.own_breaks(product)})
        table: list[tuple[int, Decimal]] = []
        for qty in points:
            price = self.unit_price(product, qty)
            if not table or price < table[-1][1]:
                table.append((qty, price))
        return table
