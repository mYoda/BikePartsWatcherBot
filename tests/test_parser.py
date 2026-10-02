from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from bike_discount import (
    ListingParseError,
    discount_percent,
    fetch_all_products,
    parse_euro_amount,
    parse_listing,
    set_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_discount_calculation_matches_display_rounding():
    percent = discount_percent(549.99, 1249.00)
    assert percent == 55.97
    assert int(round(percent)) == 56
    assert discount_percent(529.99, 1015.00) == 47.78
    assert discount_percent(100, None) is None
    assert discount_percent(100, 80) == 0.0
    assert discount_percent(60, 100) == 40.0


def test_german_price_text():
    assert parse_euro_amount("RRP* 1.249,00\xa0€") == 1249.00
    assert parse_euro_amount("from 529,99 €") == 529.99


def test_live_excerpt_parses_real_product_cards():
    products, next_page = parse_listing(read_fixture("live_excerpt.html"))
    assert next_page is None
    assert len(products) == 2

    lyrik = products[0]
    assert lyrik.id == "019a152f2cde737ea0f4c2df88a025e7"
    assert lyrik.sku == "20164960"
    assert lyrik.brand == "RockShox"
    assert lyrik.name == 'RockShox Lyrik Select Delta RC 29" Tapered Boost E1'
    assert lyrik.url == "https://www.bike-discount.de/en/rockshox-lyrik-select-delta-rc-29-tapered-boost-e1"
    assert lyrik.price == 529.99
    assert lyrik.rrp == 1015.00
    assert lyrik.discount_percent == discount_percent(529.99, 1015.00)
    assert lyrik.price_from is True
    assert lyrik.availability == "in_stock"
    assert products[1].id == "019a1531996072d0b27a9d48aacfdc7a"


def test_fixture_page_calculates_discount_and_next_page():
    products, next_page = parse_listing(read_fixture("page_1.html"))
    assert next_page == 2
    assert products[0].price == 549.99
    assert products[0].rrp == 1249.00
    assert products[0].discount_percent == 55.97
    assert products[0].price_from is False

    _, last_page = parse_listing(read_fixture("page_2.html"))
    assert last_page is None


def test_relative_product_url_is_made_absolute():
    products, _ = parse_listing(read_fixture("page_2.html"))
    assert products[0].url == "https://www.bike-discount.de/en/manitou-mezzer-pro"


def test_blocked_or_unknown_page_fails_clearly():
    with pytest.raises(ListingParseError, match="blocked"):
        parse_listing(
            "<html><head><title>Attention Required! | Cloudflare</title></head>"
            "<body><div id='cf-error-details'></div></body></html>"
        )
    with pytest.raises(ListingParseError, match="not recognized"):
        parse_listing("<html><head><title>Home</title></head><body><p>No listing</p></body></html>")


def test_listing_that_claims_products_but_has_no_cards_fails():
    html = """
    <html><body>
      <div class="cms-element-product-listing">
        <div class="js-listing-wrapper" data-aria-live-text="Showing 12 products."></div>
      </div>
    </body></html>
    """
    with pytest.raises(ListingParseError, match="showing 12"):
        parse_listing(html)


class FakeResponse:
    def __init__(self, text: str, url: str):
        self.text = text
        self.status_code = 200
        self.url = url


class FakeSession:
    def __init__(self, pages: dict[int, str]):
        self.pages = pages
        self.urls: list[str] = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        page = int(parse_qs(urlsplit(url).query)["p"][0])
        return FakeResponse(self.pages[page], url)


def test_pagination_follows_every_filtered_page_once():
    session = FakeSession({1: read_fixture("page_1.html"), 2: read_fixture("page_2.html")})
    sleeps: list[float] = []
    url = (
        "https://www.bike-discount.de/en/bike/forks"
        "?immediately-available=1&properties=abc%7Cdef&p=1&order=rabatt"
    )
    products = fetch_all_products(url, session=session, sleeper=sleeps.append)

    assert [product.id for product in products] == ["prod-a", "prod-b"]
    assert len(session.urls) == 2
    assert sleeps == [1.5]
    second = parse_qs(urlsplit(session.urls[1]).query)
    assert second["p"] == ["2"]
    assert second["immediately-available"] == ["1"]
    assert second["order"] == ["rabatt"]
    assert second["properties"] == ["abc|def"]


def test_repeated_page_does_not_duplicate_products():
    session = FakeSession({1: read_fixture("page_1.html"), 2: read_fixture("page_1.html")})
    products = fetch_all_products(
        "https://www.bike-discount.de/en/forks?p=1",
        session=session,
        sleeper=lambda _seconds: None,
    )
    assert [product.id for product in products] == ["prod-a"]
    assert len(session.urls) == 2


def test_set_page_replaces_only_the_page_parameter():
    url = set_page("https://www.bike-discount.de/en/forks?order=rabatt&p=4", 1)
    assert url.endswith("order=rabatt&p=1") or "order=rabatt" in url and "p=1" in url
    assert "p=4" not in url
