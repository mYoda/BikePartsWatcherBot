# Bike-Discount watcher

A small Python 3.12 watcher for one filtered Bike-Discount listing. It scrapes the Shopware product grid over HTTP, compares it with `data/products.json`, and sends a Telegram message when a product is new, back in stock, cheaper, or newly discounted by 40% or more.

Watched URL:

https://www.bike-discount.de/en/bike/bike-parts/mountain-bike-parts/mtb-forks/29-suspension-fork?immediately-available=1&properties=019264eb56a972018aa4844017e45d5c%7C019264eb56f27234b2e96fa10d92df2c&p=1&order=rabatt

## Layout

- `watcher.py` loads state, scrapes, compares, notifies, then saves.
- `bike_discount.py` downloads every page of the filtered query and parses product cards.
- `state.py` reads and writes `data/products.json` and decides which events happened.
- `telegram_notifier.py` formats messages and calls the Telegram Bot API.
- `tests/` uses saved HTML fixtures. Tests do not call Bike-Discount.

## Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

First run, and any later local check:

```bash
DRY_RUN=true python watcher.py
```

`DRY_RUN=true` scrapes the live listing, compares it with `data/products.json`, prints notifications, and does not call Telegram. It still writes `data/products.json`.

The first time that file is missing, the current catalogue is stored as a baseline and no notifications are sent. Delete `data/products.json` to take a new baseline.

Tests:

```bash
pytest
```

## GitHub Actions

`.github/workflows/watcher.yml` runs at minutes 3, 13, 23, 33, 43, and 53 of every hour, and can also be started with `workflow_dispatch`. It uses Python 3.12, runs `watcher.py`, and commits `data/products.json` only when that file's contents changed. An unchanged catalogue does not rewrite the file. `last_seen` moves only when that product's stored details change.

The workflow does not run on push, so the state commit cannot start another run.

To check Telegram without scraping, open Actions, choose Watch Bike-Discount, and run the workflow manually with **Send one Telegram test message and skip the catalogue scrape** enabled. That runs `python watcher.py --test-telegram` and does not change `data/products.json`.

Create these repository secrets:

| Secret | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Chat, group, or channel id that should receive messages |

The default branch must allow `github-actions[bot]` to push. GitHub may delay the 10-minute schedule; a manual run uses the same job.

## Events

Notifications are sent only for a change since the previous saved snapshot.

- `NEW_PRODUCT` — id was never stored.
- `BACK_IN_STOCK` — id was stored as `out_of_stock` and is in the latest result set again.
- `PRICE_DROP` — current price is lower than the stored price.
- `HOT_DEAL` — discount is at least 40%, and it was not already at least 40% in the saved snapshot.

Each product produces at most one message per run. A new product, restock, or price drop already shows the discount, so a hot deal does not send a second message for that product. A hot deal sends its own message only when the discount crosses 40% without one of those other changes.

A product that stays in the result set with the same price, RRP, and details does not notify again and does not change `products.json`. The first baseline run notifies for nothing.

Leaving the filtered result set marks a product `out_of_stock` without a message. `first_seen` stays unchanged. `last_seen` updates when the stored product changes, including when it goes out of stock or comes back.

If a Telegram send fails, the run exits non-zero and does not save the new state. The next run detects the same events and tries again. `DRY_RUN=true` still saves state after printing the messages.

## Assumptions

The page is a Shopware 6 listing. Each card is `div.card.product-box` and carries `data-product-information` with `id`, `name`, `brand`, `price`, and `sku`. The id is the stable key. The current price comes from that JSON. RRP comes from `.list-price-price` (`1.015,00 €` means 1015.00). Discount is `(RRP - price) / RRP`. The on-page discount badge only contains a `%` character, so the percentage is calculated.

The watched URL already sets `immediately-available=1`. Cards on that page do not include a separate stock label, so a product in the result set is stored as `in_stock`.

Pagination is the Shopware nav `nav.listing-pagination`. The next link exposes `data-focus-id="next"` and `data-page`. The next request keeps the original filter query and only changes `p`. A repeated page, an empty later page, or a missing next link stops the crawl. There is a pause between pages, a browser User-Agent, a timeout, and retries for temporary HTTP failures. A Cloudflare block or a missing listing fails the run and leaves `data/products.json` untouched.
