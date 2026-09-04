import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from playwright.sync_api import sync_playwright


PRODUCT_URL = os.environ.get("PRODUCT_URL", "").strip()

# Use the default only if the secret is missing or empty.
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

    rented_values = [
        "currently rented",
        "rented",
        "unavailable",
        "not available",
        "out of stock",
    ]

    available_values = [
        "available",
        "rent now",
        "add to cart",
        "borrow",
    ]

    if any(value in text for value in rented_values):
        return "Currently rented"

    if any(value in text for value in available_values):
        return "Available"

    words = text.replace("\n", " ").split()

    if "rent" in words:
        return "Available"

    return "Unknown"


def extract_price(text):
    prices = re.findall(
        r"\d[\d,]*\s*(?:egp|le)",
        text,
        flags=re.IGNORECASE,
    )

    return ", ".join(dict.fromkeys(prices))


def extract_games():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            print(f"Opening: {URL}")

            page.goto(
                URL,
                wait_until="networkidle",
                timeout=60_000,
            )

            page.wait_for_timeout(3_000)

            selectors = [
                "article",
                "[class*='card']",
                "[class*='game']",
                "[class*='product']",
                "li",
            ]

            cards = []

            for selector in selectors:
                found = page.locator(selector)
                count = found.count()

                if count >= 2:
                    cards = [
                        found.nth(index)
                        for index in range(count)
                    ]
                    break

            if not cards:
                print("No game cards found.")
                return {}

            games = {}

            for card in cards:
                try:
                    raw_text = card.inner_text()
                    text = clean_text(raw_text)
                except Exception:
                    continue

                if len(text) < 10:
                    continue

                lower_text = text.lower()

                if not any(
                    word in lower_text
                    for word in [
                        "rented",
                        "available",
                        "rent",
                        "egp",
                        "days",
                    ]
                ):
                    continue

                lines = [
                    clean_text(line)
                    for line in raw_text.splitlines()
                    if clean_text(line)
                ]

                if not lines:
                    continue

                title = None

                ignored_words = [
                    "rented",
                    "available",
                    "rental",
                    "price",
                    "account option",
                    "primary",
                    "secondary",
                    "days",
                    "egp",
                    "rent",
                    "buy now",
                ]

                for line in lines:
                    lower_line = line.lower()

                    if any(
                        ignored_word in lower_line
                        for ignored_word in ignored_words
                    ):
                        continue

                    if len(line) >= 2:
                        title = line
                        break

                if not title:
                    continue

                status = detect_status(text)
                price = extract_price(text)
                key = title.lower()

                games[key] = {
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

    new_games = []
    status_changes = []

    current_games = extract_games()

    if not current_games:
        print("No games extracted.")
        print("Existing state was not changed.")
        return

    for key, game in current_games.items():
        if key not in old_games:
            # The first successful run creates the baseline.
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
            old_status = old_games[key].get("status")
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
    state["last_checked"] = datetime.now(timezone.utc).isoformat()

    save_state(state)

    if new_games:
        body = "New game added to the rental page:\n\n"

        for game in new_games:
            body += (
                f"🆕 {game['title']}\n"
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

    body = "Daily rental games report\n"
    body += "=" * 30
    body += "\n\n"

    if pending_changes:
        body += "CHANGES SINCE THE LAST REPORT\n"
        body += "-" * 30
        body += "\n\n"

        for change in pending_changes:
            if change["type"] == "new":
                body += (
                    f"🆕 NEW GAME: {change['title']}\n"
                    f"Status: {change['status']}\n"
                    f"Price: {change['price'] or 'Not found'}\n\n"
                )
            else:
                body += (
                    f"🔄 STATUS CHANGED: {change['title']}\n"
                    f"{change['old_status']} → "
                    f"{change['new_status']}\n"
                    f"Price: {change['price'] or 'Not found'}\n\n"
                )
    else:
        body += "No changes since the last report.\n\n"

    body += "ALL GAMES\n"
    body += "-" * 30
    body += "\n\n"

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
    state["last_report"] = datetime.now(timezone.utc).isoformat()

    save_state(state)

    print("Daily report sent.")


if __name__ == "__main__":
    mode = os.environ.get("CHECK_MODE", "check").strip().lower()

    if mode == "daily":
        send_daily_report()
    else:
        check_games()
