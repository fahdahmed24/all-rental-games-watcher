import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from playwright.sync_api import sync_playwright


PRODUCT_URL = os.environ.get("PRODUCT_URL", "").strip()
URL = PRODUCT_URL or "https://samuraistore.site/rental-games"

if not URL.startswith(("http://", "https://")):
    raise ValueError(f"Invalid PRODUCT_URL: {URL!r}")

STATE_FILE = "all_games_state.json"

GMAIL_USERNAME = os.environ["GMAIL_USERNAME"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO = os.environ["ALERT_TO"]


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "games": {},
            "pending_changes": [],
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as error:
        print(f"Could not load state file: {error}")
        return {
            "games": {},
            "pending_changes": [],
        }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            indent=2,
            ensure_ascii=False,
        )


def send_email(subject, body):
    message = EmailMessage()
    message["From"] = GMAIL_USERNAME
    message["To"] = ALERT_TO
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(
            GMAIL_USERNAME,
            GMAIL_APP_PASSWORD,
        )
        smtp.send_message(message)


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()


def detect_status(text):
    text = text.lower()

    rented_words = [
        "currently rented",
        "rented",
        "unavailable",
        "not available",
        "out of stock",
    ]

    available_words = [
        "available",
        "rent now",
        "add to cart",
        "borrow",
    ]

    if any(word in text for word in rented_words):
        return "Currently rented"

    if any(word in text for word in available_words):
        return "Available"

    if "rent" in text.split():
        return "Available"

    return "Unknown"


def extract_price(text):
    prices = re.findall(
        r"\d[\d,]*\s*(?:egp|le)",
        text,
        flags=re.IGNORECASE,
    )

    return ", ".join(dict.fromkeys(prices))


def valid_title(text):
    if not text:
        return False

    text = clean_text(text)

    if len(text) < 2 or len(text) > 150:
        return False

    ignored_words = [
        "rented",
        "available",
        "rental",
        "price",
        "rent",
        "buy now",
        "add to cart",
        "view product",
        "read more",
        "days",
        "egp",
        "login",
        "register",
        "shop",
        "home",
    ]

    lower_text = text.lower()

    if any(word in lower_text for word in ignored_words):
        return False

    return True


def extract_games():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            viewport={
                "width": 1920,
                "height": 1080,
            }
        )

        try:
            print(f"Opening: {URL}")

            page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            page.wait_for_timeout(5_000)

            # Scroll to load games that appear lazily.
            previous_height = 0

            for _ in range(20):
                current_height = page.evaluate(
                    "document.body.scrollHeight"
                )

                page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )

                page.wait_for_timeout(1_000)

                if current_height == previous_height:
                    break

                previous_height = current_height

            # Specific selectors are intentionally before generic selectors.
            selectors = [
                "li.product",
                "article.product",
                ".product-grid-item",
                ".product-card",
                ".game-card",
                "[data-product-id]",
                "[data-product]",
                "[class*='product-card']",
                "[class*='game-card']",
                "[class*='product-item']",
                "li[class*='product']",
                "article[class*='product']",
            ]

            best_selector = None
            best_count = 0

            for selector in selectors:
                count = page.locator(selector).count()

                if count > best_count:
                    best_selector = selector
                    best_count = count

            if not best_selector or best_count == 0:
                print("No product cards were found.")

                with open(
                    "debug_page.html",
                    "w",
                    encoding="utf-8",
                ) as file:
                    file.write(page.content())

                return {}

            print(
                f"Using selector: {best_selector}"
            )
            print(
                f"Product cards found: {best_count}"
            )

            cards = page.locator(best_selector)
            games = {}

            for index in range(best_count):
                card = cards.nth(index)

                try:
                    raw_text = card.inner_text()
                except Exception:
                    continue

                text = clean_text(raw_text)

                if len(text) < 10:
                    continue

                title = None

                title_selectors = [
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    ".product-title",
                    ".game-title",
                    "[class*='product-title']",
                    "[class*='game-title']",
                    "a",
                ]

                for title_selector in title_selectors:
                    title_elements = card.locator(title_selector)
                    title_count = title_elements.count()

                    for title_index in range(title_count):
                        try:
                            candidate = clean_text(
                                title_elements
                                .nth(title_index)
                                .inner_text()
                            )
                        except Exception:
                            continue

                        if valid_title(candidate):
                            title = candidate
                            break

                    if title:
                        break

                if not title:
                    lines = [
                        clean_text(line)
                        for line in raw_text.splitlines()
                        if clean_text(line)
                    ]

                    for line in lines:
                        if valid_title(line):
                            title = line
                            break

                if not title:
                    continue

                status = detect_status(text)
                price = extract_price(text)
                game_key = title.lower()

                games[game_key] = {
                    "title": title,
                    "status": status,
                    "price": price,
                    "details": text,
                }

            print(f"Found {len(games)} games.")
            return games

        finally:
            browser.close()


def check_games():
    state = load_state()

    old_games = state.get("games", {})
    pending_changes = state.get("pending_changes", [])

    current_games = extract_games()

    if not current_games:
        print("No games extracted.")
        print("The old state was not changed.")
        return

    new_games = []
    status_changes = []

    for game_key, game in current_games.items():
        if game_key not in old_games:
            # The first run creates the baseline.
            # Existing games are not marked as new.
            if old_games:
                new_games.append(game)

                pending_changes.append(
                    {
                        "type": "new",
                        "title": game["title"],
                        "status": game["status"],
                        "price": game["price"],
                    }
                )

        else:
            old_status = old_games[game_key].get(
                "status",
                "Unknown",
            )

            new_status = game["status"]

            if (
                old_status != new_status
                and old_status != "Unknown"
                and new_status != "Unknown"
            ):
                change = {
                    "type": "status",
                    "title": game["title"],
                    "old_status": old_status,
                    "new_status": new_status,
                    "price": game["price"],
                }

                status_changes.append(change)
                pending_changes.append(change)

    state["games"] = current_games
    state["pending_changes"] = pending_changes
    state["last_checked"] = datetime.now(
        timezone.utc
    ).isoformat()

    save_state(state)

    if new_games:
        body = "🆕 NEW GAMES ADDED\n\n"

        for game in new_games:
            body += (
                f"Game: {game['title']}\n"
                f"Status: {game['status']}\n"
                f"Price: {game['price'] or 'Not found'}\n\n"
            )

        send_email(
            "🆕 New rental game added",
            body,
        )

        print("New-game email sent.")

    if status_changes:
        print("Status changes detected:")

        for change in status_changes:
            print(
                f"{change['title']}: "
                f"{change['old_status']} -> "
                f"{change['new_status']}"
            )

    print("Game check completed.")


def send_daily_report():
    state = load_state()

    games = state.get("games", {})
    pending_changes = state.get("pending_changes", [])

    if not games:
        print("No saved games.")
        print("Daily report was not sent.")
        return

    body = "DAILY RENTAL GAMES REPORT\n"
    body += "=" * 35
    body += "\n\n"

    new_games = [
        change
        for change in pending_changes
        if change.get("type") == "new"
    ]

    status_changes = [
        change
        for change in pending_changes
        if change.get("type") == "status"
    ]

    if new_games:
        body += "🆕 NEW GAMES\n"
        body += "-" * 35
        body += "\n"

        for game in new_games:
            body += (
                f"{game['title']}\n"
                f"Status: {game['status']}\n"
                f"Price: {game['price'] or 'Not found'}\n\n"
            )

    if status_changes:
        body += "🔄 CHANGED STATUS\n"
        body += "-" * 35
        body += "\n"

        for change in status_changes:
            body += (
                f"{change['title']}\n"
                f"{change['old_status']} → "
                f"{change['new_status']}\n"
                f"Price: {change['price'] or 'Not found'}\n\n"
            )

    if not pending_changes:
        body += "No changes since the last report.\n\n"

    body += "ALL GAMES\n"
    body += "-" * 35
    body += "\n"

    sorted_games = sorted(
        games.values(),
        key=lambda game: game["title"].lower(),
    )

    for game in sorted_games:
        body += (
            f"{game['title']}\n"
            f"Status: {game['status']}\n"
            f"Price: {game['price'] or 'Not found'}\n\n"
        )

    send_email(
        "Daily rental games report",
        body,
    )

    state["pending_changes"] = []
    state["last_report"] = datetime.now(
        timezone.utc
    ).isoformat()

    save_state(state)

    print("Daily report sent.")


if __name__ == "__main__":
    mode = os.environ.get(
        "CHECK_MODE",
        "check",
    ).strip().lower()

    if mode == "daily":
        send_daily_report()
    else:
        check_games()
