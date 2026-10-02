"""Format catalogue events and send them through the Telegram Bot API."""

from __future__ import annotations

import logging
import time

import requests

from state import Event

logger = logging.getLogger("telegram_notifier")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TEST_MESSAGE = (
    "✅ BikePartsWatcher test successful\n"
    "\n"
    "GitHub Actions can send Telegram notifications."
)


class TelegramError(RuntimeError):
    """A Telegram request failed. The caller must not acknowledge the change."""


def format_eur(amount: float) -> str:
    return f"€{float(amount):,.2f}"


def format_event(event: Event) -> str:
    product = event.product
    headers = {
        "NEW_PRODUCT": "🚨 NEW PRODUCT",
        "BACK_IN_STOCK": "✅ BACK IN STOCK",
        "PRICE_DROP": "📉 PRICE DROP",
        "HOT_DEAL": "🔥 HOT DEAL",
    }
    lines = [headers.get(event.type, event.type), "", _heading(product), ""]
    price = format_eur(product["price"])
    if product.get("price_from"):
        lines.append(f"💶 from {price}")
    else:
        lines.append(f"💶 {price}")
    if event.type == "PRICE_DROP" and event.previous_price is not None:
        lines.append(f"Was: {format_eur(event.previous_price)}")
    if product.get("rrp") is not None:
        lines.append(f"RRP: {format_eur(product['rrp'])}")
    discount = product.get("discount_percent")
    if discount is not None and float(discount) > 0:
        lines.append(f"🔥 -{int(round(float(discount)))}%")
    lines.extend(["", "Bike-Discount", "", product["url"]])
    return "\n".join(lines)


def deliver_events(
    events: list[Event],
    token: str | None,
    chat_id: str | None,
    dry_run: bool,
) -> int:
    """Send or print every event. Returns how many deliveries failed."""
    if not events:
        return 0
    if dry_run:
        for event in events:
            print(format_event(event))
            print()
        logger.info("DRY_RUN is enabled. Printed %s notification(s); Telegram was not called.", len(events))
        return 0
    if not token or not chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required to send notifications. "
            "Nothing was sent."
        )
        return len(events)

    failures = 0
    for index, event in enumerate(events):
        try:
            send_telegram(format_event(event), token, chat_id)
        except (TelegramError, requests.RequestException) as exc:
            failures += 1
            logger.error("Telegram notification failed: %s", exc)
        if index < len(events) - 1:
            time.sleep(0.35)
    if failures:
        logger.error(
            "%s Telegram notification(s) failed. The product state will not be acknowledged.",
            failures,
        )
    return failures


def send_test_message(token: str | None, chat_id: str | None, dry_run: bool = False) -> None:
    """Send one fixed message so a manual run can prove Telegram delivery."""
    if dry_run:
        print(TEST_MESSAGE)
        print()
        logger.info("DRY_RUN is enabled. Printed the Telegram test message; Telegram was not called.")
        return
    if not token or not chat_id:
        raise TelegramError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required to send the test message."
        )
    send_telegram(TEST_MESSAGE, token, chat_id)


def send_telegram(text: str, token: str, chat_id: str, session: requests.Session | None = None) -> None:
    url = API_URL.format(token=token)
    client = session or requests
    try:
        response = client.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        raise TelegramError(f"Telegram request failed: {exc}") from exc

    description = _error_description(response)
    if response.status_code != 200:
        raise TelegramError(f"Telegram HTTP {response.status_code}: {description}")
    try:
        body = response.json()
    except ValueError as exc:
        raise TelegramError(f"Telegram returned a non-JSON response: {description}") from exc
    if not body.get("ok"):
        raise TelegramError(f"Telegram API error: {body.get('description') or description}")


def _heading(product: dict) -> str:
    brand = str(product.get("brand") or "").strip()
    name = str(product.get("name") or "").strip()
    if brand and name.startswith(brand + " "):
        return f"{brand}\n{name[len(brand):].strip()}"
    return name


def _error_description(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:300]
    if isinstance(body, dict) and body.get("description"):
        return str(body["description"])[:300]
    return (response.text or "").strip()[:300]
