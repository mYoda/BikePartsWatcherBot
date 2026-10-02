import json
import logging
from datetime import datetime, timezone

from bike_discount import discount_percent
from state import Event, Product, canonical_state, compare_catalogue, digest_is_due, load_state, save_state
from telegram_notifier import TEST_MESSAGE, TelegramError, format_digest, format_event
import telegram_notifier
import watcher


NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


def product(**overrides) -> Product:
    data = {
        "id": "prod-a",
        "name": 'RockShox ZEB Ultimate 29" / 170 mm',
        "brand": "RockShox",
        "sku": "20164523",
        "url": "https://www.bike-discount.de/en/rockshox-zeb-ultimate",
        "price": 549.99,
        "rrp": 1249.00,
        "availability": "in_stock",
        "price_from": False,
    }
    data.update(overrides)
    data["discount_percent"] = discount_percent(data["price"], data["rrp"])
    return Product(**data)


def records(result) -> dict:
    return result.state["products"]


def test_first_run_stores_baseline_and_does_not_notify():
    hot = product()
    quiet = product(id="prod-b", name="Manitou Mezzer Pro", brand="Manitou", price=90, rrp=100)
    result = compare_catalogue(None, [hot, quiet], NOW)

    assert result.baseline is True
    assert result.events == []
    assert result.counts["found"] == 2
    assert set(records(result)) == {"prod-a", "prod-b"}
    saved = records(result)["prod-a"]
    assert saved["first_seen"] == "2026-10-02T08:00:00Z"
    assert saved["last_seen"] == "2026-10-02T08:00:00Z"
    assert saved["availability"] == "in_stock"
    assert saved["discount_percent"] == 55.97


def test_new_product_is_detected_after_baseline():
    baseline = compare_catalogue(None, [product()], NOW)
    new = product(id="prod-new", name="Fox 36 Factory", brand="Fox", price=80, rrp=100, sku="999")
    result = compare_catalogue(baseline.state, [product(), new], NOW)

    assert [event.type for event in result.events] == ["NEW_PRODUCT"]
    assert result.events[0].product["id"] == "prod-new"
    assert result.counts["new"] == 1
    assert records(result)["prod-a"]["first_seen"] == "2026-10-02T08:00:00Z"


def test_price_drop_is_detected_once():
    baseline = compare_catalogue(None, [product(price=549.99)], NOW)
    cheaper = product(price=499.99)
    result = compare_catalogue(baseline.state, [cheaper], NOW)

    assert [event.type for event in result.events] == ["PRICE_DROP"]
    assert result.events[0].previous_price == 549.99
    assert result.events[0].product["price"] == 499.99

    again = compare_catalogue(result.state, [cheaper], NOW)
    assert again.events == []


def test_back_in_stock_when_a_missing_product_returns():
    baseline = compare_catalogue(None, [product(), product(id="prod-b", price=80, rrp=100)], NOW)
    missing = compare_catalogue(baseline.state, [product(id="prod-b", price=80, rrp=100)], NOW)

    assert missing.events == []
    assert records(missing)["prod-a"]["availability"] == "out_of_stock"
    assert records(missing)["prod-a"]["last_seen"] == "2026-10-02T08:00:00Z"

    returned = compare_catalogue(missing.state, [product(), product(id="prod-b", price=80, rrp=100)], NOW)
    assert [event.type for event in returned.events] == ["BACK_IN_STOCK"]
    assert returned.events[0].product["id"] == "prod-a"
    assert records(returned)["prod-a"]["availability"] == "in_stock"
    assert records(returned)["prod-a"]["first_seen"] == "2026-10-02T08:00:00Z"


def test_unchanged_catalogue_sends_nothing_including_existing_hot_deals():
    baseline = compare_catalogue(None, [product()], NOW)
    later = datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc)
    result = compare_catalogue(baseline.state, [product()], later)
    assert result.events == []
    assert result.counts == {
        "found": 1,
        "previous": 1,
        "new": 0,
        "price_drops": 0,
        "back_in_stock": 0,
        "hot_deals": 0,
    }


def test_unchanged_catalogue_does_not_change_serialized_state():
    baseline = compare_catalogue(None, [product()], NOW)
    later = datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc)
    result = compare_catalogue(baseline.state, [product()], later)

    assert result.events == []
    assert canonical_state(result.state) == canonical_state(baseline.state)
    assert records(result)["prod-a"]["last_seen"] == "2026-10-02T08:00:00Z"


def test_price_drop_into_hot_deal_produces_one_notification():
    baseline = compare_catalogue(None, [product(price=70, rrp=100)], NOW)
    assert records(baseline)["prod-a"]["discount_percent"] == 30.0

    dropped = product(price=60, rrp=100)
    result = compare_catalogue(baseline.state, [dropped], NOW)
    assert [event.type for event in result.events] == ["PRICE_DROP"]
    text = format_event(result.events[0])
    assert text.startswith("📉 PRICE DROP")
    assert "Was: €70.00" in text
    assert "🔥 -40%" in text
    assert "HOT DEAL" not in text

    still_hot = compare_catalogue(result.state, [dropped], NOW)
    assert still_hot.events == []


def test_hot_deal_alone_when_discount_crosses_without_a_price_drop():
    baseline = compare_catalogue(None, [product(price=70, rrp=100)], NOW)
    wider_rrp = product(price=70, rrp=140)
    result = compare_catalogue(baseline.state, [wider_rrp], NOW)
    assert [event.type for event in result.events] == ["HOT_DEAL"]
    assert "🔥 -50%" in format_event(result.events[0])


def test_new_hot_product_produces_one_notification():
    baseline = compare_catalogue(None, [product(price=90, rrp=100)], NOW)
    hot = product(id="prod-hot", price=549.99, rrp=1249)
    result = compare_catalogue(baseline.state, [product(price=90, rrp=100), hot], NOW)
    assert [event.type for event in result.events] == ["NEW_PRODUCT"]
    text = format_event(result.events[0])
    assert text.startswith("🚨 NEW PRODUCT")
    assert "🔥 -56%" in text
    assert "HOT DEAL" not in text


def test_notification_text_includes_price_rrp_and_rounded_discount():
    text = format_event(Event("NEW_PRODUCT", product().to_record("t", "t")))
    assert text == (
        "🚨 NEW PRODUCT\n"
        "\n"
        "RockShox\n"
        'ZEB Ultimate 29" / 170 mm\n'
        "\n"
        "💶 €549.99\n"
        "RRP: €1,249.00\n"
        "🔥 -56%\n"
        "\n"
        "Bike-Discount\n"
        "\n"
        "https://www.bike-discount.de/en/rockshox-zeb-ultimate"
    )


def test_state_round_trip(tmp_path):
    path = tmp_path / "products.json"
    result = compare_catalogue(None, [product()], NOW)
    save_state(path, result.state)
    loaded = load_state(path)
    assert loaded == json.loads(path.read_text(encoding="utf-8"))
    assert loaded["products"]["prod-a"]["price"] == 549.99


def test_first_run_and_unchanged_run_do_not_notify(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    state_path = tmp_path / "products.json"
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setattr(watcher, "fetch_all_products", lambda *_args, **_kwargs: [product()])

    assert watcher.main() == 0
    assert "Stored 1 products as initial baseline" in caplog.text
    assert "No notifications sent" in caplog.text
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["products"]["prod-a"]["url"].startswith("https://www.bike-discount.de/")

    before = state_path.read_text(encoding="utf-8")
    caplog.clear()
    assert watcher.main() == 0
    assert "New: 0" in caplog.text
    assert "Price drops: 0" in caplog.text
    assert "Back in stock: 0" in caplog.text
    assert "Hot deals: 0" in caplog.text
    assert "Product state unchanged" in caplog.text
    assert state_path.read_text(encoding="utf-8") == before


def test_telegram_failure_does_not_acknowledge_new_product(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    state_path = tmp_path / "products.json"
    save_state(state_path, compare_catalogue(None, [product(price=90, rrp=100)], NOW).state)
    before = state_path.read_text(encoding="utf-8")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(telegram_notifier.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        watcher,
        "fetch_all_products",
        lambda *_args, **_kwargs: [product(price=90, rrp=100), product(id="prod-new", price=80, rrp=100)],
    )

    def explode(*_args, **_kwargs):
        raise TelegramError("bot was blocked by the user")

    monkeypatch.setattr("telegram_notifier.send_telegram", explode)

    assert watcher.main() == 1
    assert "Product state was not saved" in caplog.text
    assert state_path.read_text(encoding="utf-8") == before
    assert "prod-new" not in json.loads(before)["products"]


def test_next_run_after_telegram_failure_detects_the_event_again(tmp_path, monkeypatch):
    state_path = tmp_path / "products.json"
    save_state(state_path, compare_catalogue(None, [product(price=90, rrp=100)], NOW).state)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(telegram_notifier.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        watcher,
        "fetch_all_products",
        lambda *_args, **_kwargs: [product(price=90, rrp=100), product(id="prod-new", price=80, rrp=100)],
    )
    attempts = {"count": 0}

    def fail_once(*_args, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TelegramError("temporary network error")

    monkeypatch.setattr("telegram_notifier.send_telegram", fail_once)

    assert watcher.main() == 1
    assert "prod-new" not in json.loads(state_path.read_text(encoding="utf-8"))["products"]

    assert watcher.main() == 0
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "prod-new" in saved["products"]
    assert attempts["count"] == 2


def test_scrape_failure_does_not_touch_state(tmp_path, monkeypatch):
    from bike_discount import ListingParseError

    state_path = tmp_path / "products.json"
    original = compare_catalogue(None, [product()], NOW).state
    save_state(state_path, original)
    before = state_path.read_text(encoding="utf-8")
    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setenv("DRY_RUN", "true")

    def fail(*_args, **_kwargs):
        raise ListingParseError("Bike-Discount page structure was not recognized")

    monkeypatch.setattr(watcher, "fetch_all_products", fail)
    assert watcher.main() == 1
    assert state_path.read_text(encoding="utf-8") == before


def test_test_telegram_sends_one_message_and_leaves_state_untouched(tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "products.json"
    save_state(state_path, compare_catalogue(None, [product()], NOW).state)
    before = state_path.read_text(encoding="utf-8")
    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.delenv("DRY_RUN", raising=False)
    sent = []

    def capture(text, token, chat_id, session=None):
        sent.append((text, token, chat_id))

    def scrape(*_args, **_kwargs):
        raise AssertionError("the catalogue must not be scraped")

    monkeypatch.setattr("telegram_notifier.send_telegram", capture)
    monkeypatch.setattr(watcher, "fetch_all_products", scrape)
    monkeypatch.setattr(watcher, "save_state", scrape)

    assert watcher.main(["--test-telegram"]) == 0
    assert sent == [(TEST_MESSAGE, "token", "123")]
    assert TEST_MESSAGE == (
        "✅ BikePartsWatcher test successful\n"
        "\n"
        "GitHub Actions can send Telegram notifications."
    )
    assert state_path.read_text(encoding="utf-8") == before
    assert capsys.readouterr().out == ""


def test_test_telegram_failure_does_not_touch_state(tmp_path, monkeypatch):
    state_path = tmp_path / "products.json"
    save_state(state_path, compare_catalogue(None, [product()], NOW).state)
    before = state_path.read_text(encoding="utf-8")
    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.delenv("DRY_RUN", raising=False)

    def explode(*_args, **_kwargs):
        raise TelegramError("chat not found")

    monkeypatch.setattr("telegram_notifier.send_telegram", explode)
    assert watcher.main(["--test-telegram"]) == 1
    assert state_path.read_text(encoding="utf-8") == before


def digest_catalogue() -> list[Product]:
    return [
        product(id="low", name="Low Fork", price=90, rrp=100, url="https://example.test/low"),
        product(
            id="best",
            name="RockShox ZEB Ultimate",
            price=549.99,
            rrp=1149,
            url="https://example.test/best",
        ),
        product(id="mid", name="Mid Fork", price=60, rrp=100, url="https://example.test/mid"),
        product(id="second", name="Second Fork", price=50, rrp=100, url="https://example.test/second"),
        product(id="plain", name="No RRP Fork", price=100, rrp=None, url="https://example.test/plain"),
    ]


def test_digest_lists_top_3_deals_by_discount_descending():
    text = format_digest(
        [
            {
                "name": item.name,
                "price": item.price,
                "rrp": item.rrp,
                "discount_percent": item.discount_percent,
                "url": item.url,
            }
            for item in digest_catalogue()
        ]
    )
    assert text.startswith("✅ BikePartsWatcher alive\n")
    assert "Bike-Discount checked successfully\n5 products currently available" in text
    assert "🔥 Top deals now:" in text
    assert text.index("1. RockShox ZEB Ultimate") < text.index("2. Second Fork") < text.index("3. Mid Fork")
    assert "€549.99 / RRP €1,149.00" in text
    assert "🔥 -52%" in text
    assert "https://example.test/best" in text
    assert "Low Fork" not in text
    assert "No RRP Fork" not in text


def test_digest_is_due_once_per_interval():
    noon = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    assert digest_is_due(None, noon) is True
    assert digest_is_due("2026-10-02T11:01:00Z", noon) is False
    assert digest_is_due("2026-10-02T11:00:00Z", noon) is True


def _schedule_digest(tmp_path, monkeypatch, last_digest_at: str | None, now: datetime):
    products = digest_catalogue()
    state = compare_catalogue(None, products, NOW).state
    if last_digest_at:
        state["last_digest_at"] = last_digest_at
    state_path = tmp_path / "products.json"
    save_state(state_path, state)
    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setattr(watcher, "utc_now", lambda: now)
    monkeypatch.setattr(watcher, "fetch_all_products", lambda *_args, **_kwargs: products)
    return state_path


def test_digest_sent_when_due(tmp_path, monkeypatch):
    noon = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    state_path = _schedule_digest(tmp_path, monkeypatch, "2026-10-02T10:00:00Z", noon)
    sent = []

    def capture(text, token, chat_id, session=None):
        sent.append(text)

    monkeypatch.setattr("telegram_notifier.send_telegram", capture)
    assert watcher.main() == 0
    assert len(sent) == 1
    assert sent[0].startswith("✅ BikePartsWatcher alive")
    assert "1. RockShox ZEB Ultimate" in sent[0]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["last_digest_at"] == "2026-10-02T12:00:00Z"


def test_digest_not_sent_again_within_the_same_hour(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    noon = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    state_path = _schedule_digest(tmp_path, monkeypatch, "2026-10-02T11:20:00Z", noon)
    before = state_path.read_text(encoding="utf-8")

    def explode(*_args, **_kwargs):
        raise AssertionError("digest must not be sent inside the same hour")

    monkeypatch.setattr("telegram_notifier.send_telegram", explode)
    assert watcher.main() == 0
    assert "Digest not due" in caplog.text
    assert state_path.read_text(encoding="utf-8") == before


def test_failed_digest_is_retried_on_the_next_run(tmp_path, monkeypatch):
    noon = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    state_path = _schedule_digest(tmp_path, monkeypatch, "2026-10-02T10:00:00Z", noon)
    attempts = {"count": 0}

    def fail_once(*_args, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TelegramError("temporary network error")

    monkeypatch.setattr("telegram_notifier.send_telegram", fail_once)
    assert watcher.main() == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["last_digest_at"] == "2026-10-02T10:00:00Z"

    assert watcher.main() == 0
    assert json.loads(state_path.read_text(encoding="utf-8"))["last_digest_at"] == "2026-10-02T12:00:00Z"
    assert attempts["count"] == 2
