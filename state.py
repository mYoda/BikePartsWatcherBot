"""Load, compare, and save the watched product catalogue."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOT_DEAL_PERCENT = 40.0
# Change this to 6 to send the digest once every six hours.
DIGEST_INTERVAL_HOURS = 1


@dataclass
class Product:
    id: str
    name: str
    url: str
    price: float
    availability: str
    brand: str | None = None
    sku: str | None = None
    rrp: float | None = None
    discount_percent: float | None = None
    price_from: bool = False

    def to_record(self, first_seen: str, last_seen: str) -> dict:
        return {
            "id": self.id,
            "sku": self.sku,
            "name": self.name,
            "brand": self.brand,
            "url": self.url,
            "price": round(float(self.price), 2),
            "rrp": None if self.rrp is None else round(float(self.rrp), 2),
            "discount_percent": self.discount_percent,
            "availability": self.availability,
            "price_from": bool(self.price_from),
            "first_seen": first_seen,
            "last_seen": last_seen,
        }


@dataclass
class Event:
    type: str
    product: dict
    previous_price: float | None = None


@dataclass
class CompareResult:
    state: dict
    events: list[Event] = field(default_factory=list)
    baseline: bool = False
    counts: dict = field(default_factory=dict)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"State file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("products"), dict):
        raise RuntimeError(f"State file {path} must contain a 'products' object.")
    return data


def canonical_state(state: dict) -> str:
    payload = {"products": {key: state["products"][key] for key in sorted(state["products"])}}
    if state.get("last_digest_at"):
        payload["last_digest_at"] = state["last_digest_at"]
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def digest_is_due(last_digest_at: str | None, now: datetime) -> bool:
    if not last_digest_at:
        return True
    sent_at = datetime.fromisoformat(str(last_digest_at).replace("Z", "+00:00"))
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return now - sent_at >= timedelta(hours=DIGEST_INTERVAL_HOURS)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(canonical_state(state), encoding="utf-8")
    temporary.replace(path)


def compare_catalogue(
    previous: dict | None,
    products: list[Product],
    now: datetime | None = None,
) -> CompareResult:
    stamp = isoformat(now or utc_now())
    found = len(products)

    if previous is None:
        records = {
            product.id: product.to_record(stamp, stamp)
            for product in _unique(products)
        }
        return CompareResult(
            state={"products": records},
            events=[],
            baseline=True,
            counts=_counts(found, 0, []),
        )

    stored = {key: dict(value) for key, value in previous.get("products", {}).items()}
    events: list[Event] = []

    for product in _unique(products):
        record = product.to_record(stamp, stamp)
        prior = stored.get(product.id)
        if prior is None:
            stored[product.id] = record
            events.append(Event("NEW_PRODUCT", record))
            if _is_hot(record.get("discount_percent")):
                events.append(Event("HOT_DEAL", record))
            continue

        record["first_seen"] = prior.get("first_seen") or stamp
        if _snapshot(record) == _snapshot(prior):
            stored[product.id] = prior
            continue

        stored[product.id] = record
        was_available = prior.get("availability", "in_stock") == "in_stock"
        old_price = _money(prior.get("price"))
        new_price = _money(record.get("price"))

        if not was_available:
            events.append(Event("BACK_IN_STOCK", record, previous_price=old_price))
        if old_price is not None and new_price is not None and new_price < old_price - 0.001:
            events.append(Event("PRICE_DROP", record, previous_price=old_price))
        if _is_hot(record.get("discount_percent")) and not _is_hot(prior.get("discount_percent")):
            events.append(Event("HOT_DEAL", record))

    seen = {product.id for product in products}
    for product_id, prior in stored.items():
        if product_id in seen or prior.get("availability") != "in_stock":
            continue
        prior["availability"] = "out_of_stock"
        prior["last_seen"] = stamp

    events = _one_event_per_product(events)
    previous_count = len(previous.get("products", {}))
    return CompareResult(
        state={"products": stored},
        events=events,
        baseline=False,
        counts=_counts(found, previous_count, events),
    )


def _unique(products: list[Product]) -> list[Product]:
    unique: list[Product] = []
    seen: set[str] = set()
    for product in products:
        if product.id in seen:
            continue
        seen.add(product.id)
        unique.append(product)
    return unique


_EVENT_PRIORITY = {
    "BACK_IN_STOCK": 0,
    "NEW_PRODUCT": 1,
    "PRICE_DROP": 2,
    "HOT_DEAL": 3,
}


def _one_event_per_product(events: list[Event]) -> list[Event]:
    """Keep one notification per product. A hot deal enriches a stronger event."""
    chosen: dict[str, Event] = {}
    order: list[str] = []
    for event in events:
        product_id = event.product["id"]
        current = chosen.get(product_id)
        if current is None:
            chosen[product_id] = event
            order.append(product_id)
            continue
        if _EVENT_PRIORITY[event.type] < _EVENT_PRIORITY[current.type]:
            chosen[product_id] = event
    return [chosen[product_id] for product_id in order]


def _snapshot(record: dict) -> tuple:
    discount = record.get("discount_percent")
    if discount is not None:
        discount = round(float(discount), 2)
    return (
        record.get("sku"),
        record.get("name"),
        record.get("brand"),
        record.get("url"),
        _money(record.get("price")),
        _money(record.get("rrp")),
        discount,
        record.get("availability"),
        bool(record.get("price_from")),
    )


def _is_hot(discount) -> bool:
    if discount is None:
        return False
    try:
        return float(discount) >= HOT_DEAL_PERCENT
    except (TypeError, ValueError):
        return False


def _money(value) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _counts(found: int, previous: int, events: list[Event]) -> dict:
    return {
        "found": found,
        "previous": previous,
        "new": sum(1 for event in events if event.type == "NEW_PRODUCT"),
        "price_drops": sum(1 for event in events if event.type == "PRICE_DROP"),
        "back_in_stock": sum(1 for event in events if event.type == "BACK_IN_STOCK"),
        "hot_deals": sum(1 for event in events if event.type == "HOT_DEAL"),
    }
