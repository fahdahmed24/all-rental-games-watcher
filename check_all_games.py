import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from playwright.sync_api import sync_playwright


URL = os.environ.get(
    "PRODUCT_URL",
    "https://samuraistore.site/rental-games"
)

STATE_FILE = "all_games_state.json"

GMAIL_USERNAME = os.environ["GMAIL_USERNAME"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO = os.environ["ALERT_TO"]


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "games": {},
            "pending_changes": []
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {
            "games": {},
            "pending_changes": []
        }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)


def send_email(subject, body):
    message = EmailMessage()
    message["From"] = GMAIL_USERNAME
    message["To"] = ALERT_TO
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USERNAME, GMAIL_APP_PASSWORD)
        smtp.send_message(message)


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()


def detect_status(text):
    text = text.lower()

    if any(value in text for value in [
        "currently rented",
        "rented",
        "unavailable",
        "not available",
        "out of stock"
    ]):
        return "Currently rented"

    if any(value in text for value in [
        "available",
        "rent now",
        "add to cart",
        "borrow"
    ]):
        return "Available"

    # A button or label called only "Rent" usually means available.
    words = text.replace("\n", " ").split()

    if "rent" in words:
        return "Available"

    return "Unknown"


def extract_price(text):
    prices = re.findall(
        r"\d[\d,]*\s*(?:egp|le)",
        text,
        flags=re.IGNORECASE
    )

    return ", ".join(dict.fromkeys(prices))


def extract_games():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            page.goto(URL, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(3_000)

            # Try common game-card/listing selectors.
            selectors = [
                "article",
                "[class*='card']",
                "[class*='game']",
                "[class*='product']",
                "li"
            ]

            cards = []

            for selector in selectors:
                found = page.locator(selector)
                count = found.count()

                if count >= 2:
                    cards = [found.nth(i) for i in range(count)]
                    break

            if not cards:
                print("No game cards found.")
                return {}

            games = {}

            for card in cards:
                try:
                    text = clean_text(card.inner_text())
                except Exception:
                    continue

                if len(text) < 10:
                    continue

                lower_text = text.lower()

                # Ignore page navigation and unrelated elements.
                if not any(word in lower_text for word in [
                    "rented",
                    "available",
                    "rent",
                    "egp",
                    "days"
                ]):
                    continue

                # Find a title from the first meaningful line.
                lines = [
                    clean_text(line)
                    for line in card.inner_text().splitlines()
                    if clean_text(line)
                ]

                if not lines:
                    continue

                title = None

                for line in lines:
                    lower_line = line.lower()

                    if any(skip in lower_line for skip in [
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
                        "buy now"
                    ]):
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
                    "details": text
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
        print("No games extracted. Existing state was not changed.")
        return

    for key, game in current_games.items():
        if key not in old_games:
            # Do not call all games "new" on the very first run.
            if old_games:
                new_games.append(game)

                pending_changes.append({
                    "type": "new",
                    "title": game["title"],
                    "status": game["status"],
                    "price": game["price"]
                })

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
                    "price": game["price"]
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

        send_email("🆕 New rental game added", body)
        print("New-game email sent.")

    if status_changes:
        print("Status changes detected:")

        for change in status_changes:
            print(
                f"{change['title']}: "
                f"{change['old_status']} -> {change['new_status']}"
            )

    print("Game check completed.")


def send_daily_report():
    state = load_state()
    games = state.get("games", {})
    pending_changes = state.get("pending_changes", [])

    if not games:
        print("No saved games. Daily report was not sent.")
        return

    body = "Daily rental games report\n"
    body += "=" * 30 + "\n\n"

    if pending_changes:
        body += "CHANGES SINCE THE LAST REPORT\n"
        body += "-" * 30 + "\n"

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
                    f"{change['old_status']} -> "
                    f"{change['new_status']}\n"
                    f"Price: {change['price'] or 'Not found'}\n\n"
                )
    else:
        body += "No changes since the last report.\n\n"

    body += "ALL GAMES\n"
    body += "-" * 30 + "\n"

    sorted_games = sorted(
        games.values(),
        key=lambda game: game["title"].lower()
    )

    for game in sorted_games:
        body += (
            f"{game['title']}\n"
            f"Status: {game['status']}\n"
            f"Price: {game['price'] or 'Not found'}\n\n"
        )

    send_email("Daily rental games report", body)

    # Changes have now been included in the daily report.
    state["pending_changes"] = []
    state["last_report"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    print("Daily report sent.")


if __name__ == "__main__":
    mode = os.environ.get("CHECK_MODE", "check")

    if mode == "daily":
        send_daily_report()
    else:
        check_games()
