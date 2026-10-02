#!/usr/bin/env python3
"""Watch a Bike-Discount listing and notify Telegram when it changes."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from bike_discount import DEFAULT_LISTING_URL, ListingParseError, fetch_all_products
from state import canonical_state, compare_catalogue, load_state, save_state, utc_now
from telegram_notifier import deliver_events

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("watcher")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dry_run = _env_flag("DRY_RUN")
    state_path = Path(os.environ.get("STATE_PATH", ROOT / "data" / "products.json"))
    listing_url = os.environ.get("LISTING_URL", DEFAULT_LISTING_URL).strip()

    try:
        previous = load_state(state_path)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    try:
        products = fetch_all_products(listing_url)
    except ListingParseError as exc:
        logger.error("%s", exc)
        logger.error("Scrape failed. Product state was not modified.")
        return 1
    except Exception:
        logger.exception("Unexpected scrape error. Product state was not modified.")
        return 1

    result = compare_catalogue(previous, products, utc_now())
    counts = result.counts
    logger.info("Found %s products", counts["found"])

    if result.baseline:
        logger.info("No previous state found")
        logger.info("Stored %s products as initial baseline", counts["found"])
        logger.info("No notifications sent")
    else:
        logger.info("Loaded %s previous products", counts["previous"])
        logger.info("New: %s", counts["new"])
        logger.info("Price drops: %s", counts["price_drops"])
        logger.info("Back in stock: %s", counts["back_in_stock"])
        logger.info("Hot deals: %s", counts["hot_deals"])

    failures = 0
    if result.events:
        failures = deliver_events(
            result.events,
            os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None,
            os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None,
            dry_run=dry_run,
        )
    elif dry_run and not result.baseline:
        logger.info("DRY_RUN is enabled. No notifications to send.")

    if failures:
        logger.error(
            "Telegram delivery failed for %s notification(s). "
            "Product state was not saved, so these events will be retried.",
            failures,
        )
        return 1

    if previous is not None and canonical_state(previous) == canonical_state(result.state):
        logger.info("Product state unchanged")
        return 0

    try:
        save_state(state_path, result.state)
    except OSError as exc:
        logger.error("Could not save product state to %s: %s", state_path, exc)
        return 1

    logger.info("Saved product state to %s", state_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
