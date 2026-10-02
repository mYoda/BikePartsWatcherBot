"""Fetch and parse Bike-Discount Shopware listing pages."""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from state import Product

logger = logging.getLogger("bike_discount")

DEFAULT_LISTING_URL = (
    "https://www.bike-discount.de/en/bike/bike-parts/mountain-bike-parts/"
    "mtb-forks/29-suspension-fork?immediately-available=1"
    "&properties=019264eb56a972018aa4844017e45d5c%7C019264eb56f27234b2e96fa10d92df2c"
    "&p=1&order=rabatt"
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MAX_PAGES = 40
PAGE_DELAY_SECONDS = 1.5
BASE_URL = "https://www.bike-discount.de"

_GERMAN_AMOUNT = re.compile(r"(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2})")
_DOT_AMOUNT = re.compile(r"(\d+\.\d{2})")
_SHOWING = re.compile(r"Showing\s+(\d+)", re.IGNORECASE)


class ListingParseError(RuntimeError):
    """The listing HTML is blocked or no longer matches the expected Shopware markup."""


def discount_percent(price: float | None, rrp: float | None) -> float | None:
    if price is None or rrp is None or rrp <= 0:
        return None
    if price >= rrp:
        return 0.0
    return round((rrp - price) / rrp * 100, 2)


def parse_euro_amount(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").replace(" ", "")
    match = _GERMAN_AMOUNT.search(cleaned)
    if match:
        number = match.group(1).replace(".", "").replace(",", ".")
        return round(float(number), 2)
    match = _DOT_AMOUNT.search(cleaned)
    if match:
        return round(float(match.group(1)), 2)
    return None


def set_page(url: str, page: int) -> str:
    """Return url with the p= query parameter set, preserving the other filters."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "p"]
    query.append(("p", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "no-cache",
        }
    )
    return session


def fetch_all_products(
    url: str = DEFAULT_LISTING_URL,
    session: requests.Session | None = None,
    sleeper=time.sleep,
) -> list[Product]:
    """Follow every result page that belongs to the same filtered query."""
    session = session or build_session()
    collected: list[Product] = []
    seen_ids: set[str] = set()
    seen_signatures: set[tuple[str, ...]] = set()
    page = 1

    for _ in range(MAX_PAGES):
        page_url = set_page(url, page)
        logger.info("Fetching page %s...", page)
        html = _get_html(session, page_url)
        products, next_page = parse_listing(html)
        signature = tuple(product.id for product in products)
        if signature in seen_signatures:
            logger.info("Page %s repeated an earlier result page; stopping pagination.", page)
            break
        seen_signatures.add(signature)

        added = 0
        for product in products:
            if product.id in seen_ids:
                continue
            seen_ids.add(product.id)
            collected.append(product)
            added += 1
        if added == len(products):
            logger.info("Page %s: %s products.", page, len(products))
        else:
            logger.info(
                "Page %s: %s products, %s already seen on an earlier page.",
                page,
                len(products),
                len(products) - added,
            )

        if page > 1 and not products:
            logger.info("Page %s is empty; stopping pagination.", page)
            break
        if not next_page or next_page <= page:
            break

        page = next_page
        sleeper(PAGE_DELAY_SECONDS)
    else:
        raise ListingParseError(
            f"Pagination did not end after {MAX_PAGES} pages. Refusing to continue."
        )

    return collected


def parse_listing(html: str) -> tuple[list[Product], int | None]:
    _reject_block_page(html)
    if "cms-element-product-listing" not in html and "product-box" not in html:
        title = _page_title(html)
        raise ListingParseError(
            "Bike-Discount page structure was not recognized "
            f"(title: {title!r}). The product listing markup is missing. "
            "The site may have changed, or the request was blocked."
        )

    soup = BeautifulSoup(html, "html.parser")
    if (
        soup.select_one(".cms-element-product-listing, .js-listing-wrapper") is None
        and not soup.select("div.card.product-box")
    ):
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        raise ListingParseError(
            "Bike-Discount page structure was not recognized "
            f"(title: {title!r}). Expected a Shopware product listing."
        )

    products: list[Product] = []
    seen: set[str] = set()
    for box in soup.select("div.card.product-box"):
        product = _parse_box(box)
        if product.id in seen:
            continue
        seen.add(product.id)
        products.append(product)

    expected = _expected_page_count(soup)
    if expected and expected > 0 and not products:
        raise ListingParseError(
            f"The listing says it is showing {expected} products, but no product cards "
            "could be parsed. The page structure may have changed."
        )

    return products, _next_page(soup)


def _get_html(session: requests.Session, url: str) -> str:
    try:
        response = session.get(url, timeout=(10, 30))
    except requests.RequestException as exc:
        raise ListingParseError(f"Request failed for {url}: {exc}") from exc
    if response.status_code >= 400:
        raise ListingParseError(f"Bike-Discount returned HTTP {response.status_code} for {url}.")
    text = response.text or ""
    _reject_block_page(text)
    return text


def _reject_block_page(html: str) -> None:
    head = html[:8000].lower()
    if (
        "cf-error-details" in head
        or "attention required" in head
        or "just a moment" in head
        or "cf-browser-verification" in head
    ):
        raise ListingParseError(
            "Bike-Discount blocked the request (Cloudflare challenge). "
            "Product state was not modified."
        )


def _page_title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _expected_page_count(soup: BeautifulSoup) -> int | None:
    node = soup.select_one("[data-aria-live-text]")
    if node is None:
        return None
    match = _SHOWING.search(node.get("data-aria-live-text") or "")
    if not match:
        return None
    return int(match.group(1))


def _next_page(soup: BeautifulSoup) -> int | None:
    nav = soup.select_one("nav.listing-pagination")
    if nav is None:
        return None
    link = nav.select_one("a.page-link[data-focus-id='next']")
    if link is None:
        return None
    parent = link.find_parent("li")
    parent_classes = parent.get("class") if parent is not None else []
    if parent_classes and "disabled" in parent_classes:
        return None
    if link.get("aria-disabled") == "true" or link.get("href") == "#":
        return None
    raw_page = link.get("data-page")
    if not raw_page:
        return None
    try:
        page = int(raw_page)
    except ValueError:
        return None

    active = nav.select_one("li.page-item.active a.page-link")
    if active is not None and active.get("data-page"):
        try:
            current = int(active["data-page"])
        except ValueError:
            current = None
        if current is not None and page <= current:
            return None
    return page


def _parse_box(box) -> Product:
    raw = box.get("data-product-information")
    if not raw:
        raise ListingParseError("A product card is missing data-product-information.")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ListingParseError(f"A product card has invalid JSON: {exc}") from exc
    if not isinstance(info, dict):
        raise ListingParseError("A product card JSON payload is not an object.")

    product_id = str(info.get("id") or "").strip()
    if not product_id:
        raise ListingParseError("A product card is missing a stable id.")

    brand = str(info.get("brand") or "").strip() or None
    raw_name = str(info.get("name") or "").strip()
    if brand and raw_name:
        name = f"{brand} {raw_name}"
    else:
        name = raw_name or brand or ""
    if not name:
        raise ListingParseError(f"Product {product_id} is missing a name.")

    link = box.select_one("a.product-image-link[href], .product-title a[href]")
    href = (link.get("href") if link is not None else "") or ""
    href = href.strip()
    if not href:
        raise ListingParseError(f"Product {product_id} is missing a product URL.")
    if href.startswith("/"):
        href = BASE_URL + href

    price_from, html_price = _price_from_html(box)
    raw_price = info.get("price")
    if raw_price is None or raw_price == "":
        price = html_price
    else:
        try:
            price = round(float(raw_price), 2)
        except (TypeError, ValueError) as exc:
            raise ListingParseError(f"Product {product_id} has an invalid price.") from exc
    if price is None:
        raise ListingParseError(f"Product {product_id} is missing a current price.")

    rrp = None
    list_el = box.select_one(".list-price-price")
    if list_el is not None:
        rrp = parse_euro_amount(list_el.get_text(" ", strip=True))

    sku = str(info.get("sku") or "").strip() or None
    return Product(
        id=product_id,
        sku=sku,
        name=name,
        brand=brand,
        url=href,
        price=price,
        rrp=rrp,
        discount_percent=discount_percent(price, rrp),
        availability="in_stock",
        price_from=price_from,
    )


def _price_from_html(box) -> tuple[bool, float | None]:
    price_el = box.select_one(".product-price")
    if price_el is None:
        return False, None
    text = price_el.get_text(" ", strip=True)
    price_from = re.search(r"\bfrom\b", text, flags=re.IGNORECASE) is not None
    list_el = price_el.select_one(".list-price")
    if list_el is not None:
        list_text = list_el.get_text(" ", strip=True)
        text = text.replace(list_text, " ")
    return price_from, parse_euro_amount(text)
