import os
import asyncio
import logging
import re
import socket
import sys
import threading
import time
from contextlib import nullcontext
from html import escape
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from dotenv import load_dotenv
import requests
import urllib3.util.connection as urllib3_connection
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from telegram import Update, BotCommand
from telegram.error import Conflict, Forbidden, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

def clean_env_value(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    return value.strip().strip('"').strip("'")


TELEGRAM_BOT_TOKEN = clean_env_value("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = clean_env_value("TELEGRAM_CHAT_ID")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "600"))
CHECK_TIMEOUT_SECONDS = int(os.getenv("CHECK_TIMEOUT_SECONDS", "90"))
GOTO_TIMEOUT_SECONDS = int(os.getenv("GOTO_TIMEOUT_SECONDS", "45"))
SELECTOR_TIMEOUT_SECONDS = int(os.getenv("SELECTOR_TIMEOUT_SECONDS", "30"))
NAVIGATION_RETRIES = int(os.getenv("NAVIGATION_RETRIES", "3"))
FIRST_CHECK_DELAY_SECONDS = int(os.getenv("FIRST_CHECK_DELAY_SECONDS", "180"))
URL = clean_env_value("TARGET_URL", "https://adhahi.dz/register")
WILAYA_QUOTAS_API_URL = clean_env_value(
    "WILAYA_QUOTAS_API_URL",
    "https://adhahi.dz/api/v1/public/wilaya-quotas",
)
WILAYA_QUOTAS_API_URLS = [
    url.strip()
    for url in clean_env_value(
        "WILAYA_QUOTAS_API_URLS",
        f"{WILAYA_QUOTAS_API_URL},https://www.adhahi.dz/api/v1/public/wilaya-quotas",
    ).split(",")
    if url.strip()
]
API_CONNECT_TIMEOUT_SECONDS = int(os.getenv("API_CONNECT_TIMEOUT_SECONDS", "6"))
API_TIMEOUT_SECONDS = int(os.getenv("API_TIMEOUT_SECONDS", "20"))
API_RETRIES = int(os.getenv("API_RETRIES", "3"))
API_RETRY_DELAY_SECONDS = int(os.getenv("API_RETRY_DELAY_SECONDS", "2"))
API_FORCE_IPV4 = clean_env_value("API_FORCE_IPV4", "true")
BROWSER_FALLBACK_ENABLED = clean_env_value("BROWSER_FALLBACK_ENABLED", "false")
CHROMIUM_EXECUTABLE_PATH = clean_env_value("CHROMIUM_EXECUTABLE_PATH")
ERROR_SCREENSHOT_PATH = clean_env_value("ERROR_SCREENSHOT_PATH", "")
PORT = clean_env_value("PORT")

PLACEHOLDER_VALUES = {
    "",
    "your_bot_token_here",
    "your_chat_id_here",
    "replace_me",
    "changeme",
    "placeholder",
}
BOT_TOKEN_PATTERN = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")
CHAT_ID_PATTERN = re.compile(r"^-?\d+$")

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(BASE_DIR / "monitor.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


class SensitiveDataFilter(logging.Filter):
    """Prevents secrets from being written if they appear in log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_secret(record.msg)
        if isinstance(record.args, dict):
            record.args = {
                key: _redact_secret(value) for key, value in record.args.items()
            }
        elif record.args:
            record.args = tuple(_redact_secret(value) for value in record.args)
        return True


def _redact_secret(value):
    if isinstance(value, str):
        for secret, label in (
            (TELEGRAM_BOT_TOKEN, "[REDACTED_BOT_TOKEN]"),
            (TELEGRAM_CHAT_ID, "[REDACTED_CHAT_ID]"),
        ):
            if secret:
                value = value.replace(secret, label)
    return value


for handler in logging.getLogger().handlers:
    handler.addFilter(SensitiveDataFilter())

# Keep track of wilayas we have already notified about so we don't spam
notified_wilayas: set[str] = set()
availability_lock = asyncio.Lock()
availability_check_started_at: float | None = None
availability_check_source: str | None = None


class AvailabilityCheckError(Exception):
    """Raised when the website check cannot complete reliably."""


class ConfigurationError(Exception):
    """Raised when required deployment configuration is missing or unsafe."""


def is_placeholder(value: str | None) -> bool:
    return value is None or value.strip().lower() in PLACEHOLDER_VALUES


def validate_configuration():
    """Fail early with useful logs before Telegram raises a less obvious error."""
    errors: list[str] = []

    if is_placeholder(TELEGRAM_BOT_TOKEN):
        errors.append("TELEGRAM_BOT_TOKEN is missing or still set to a placeholder.")
    elif not BOT_TOKEN_PATTERN.match(str(TELEGRAM_BOT_TOKEN)):
        errors.append("TELEGRAM_BOT_TOKEN does not look like a valid Telegram bot token.")

    if is_placeholder(TELEGRAM_CHAT_ID):
        errors.append("TELEGRAM_CHAT_ID is missing or still set to a placeholder.")
    elif not CHAT_ID_PATTERN.match(str(TELEGRAM_CHAT_ID)):
        errors.append("TELEGRAM_CHAT_ID must be the numeric recipient chat id.")

    if (
        TELEGRAM_BOT_TOKEN
        and TELEGRAM_CHAT_ID
        and CHAT_ID_PATTERN.match(str(TELEGRAM_CHAT_ID))
    ):
        bot_user_id = str(TELEGRAM_BOT_TOKEN).split(":", 1)[0]
        if str(TELEGRAM_CHAT_ID) == bot_user_id:
            errors.append(
                "TELEGRAM_CHAT_ID is set to the bot's own user id. Bots cannot send messages to themselves."
            )

    if errors:
        raise ConfigurationError(" ".join(errors))


def env_flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class force_ipv4_requests:
    """Temporarily make urllib3 resolve only IPv4 addresses for Requests."""

    def __enter__(self):
        self.original_allowed_gai_family = urllib3_connection.allowed_gai_family
        urllib3_connection.allowed_gai_family = lambda: socket.AF_INET

    def __exit__(self, exc_type, exc, traceback):
        urllib3_connection.allowed_gai_family = self.original_allowed_gai_family


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, format, *args):
        logger.debug("Health check: " + format, *args)


def start_health_server():
    """Expose a tiny HTTP endpoint for hosts that require a listening port."""
    if not PORT:
        return

    try:
        port = int(PORT)
    except ValueError:
        logger.warning("Ignoring invalid PORT value: %s", PORT)
        return

    def serve():
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        logger.info("Health server listening on port %d", port)
        server.serve_forever()

    threading.Thread(target=serve, daemon=True).start()


def is_authorized(update: Update) -> bool:
    """Allow only the configured Telegram chat to use bot commands."""
    return (
        TELEGRAM_CHAT_ID is not None
        and update.effective_chat is not None
        and str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)
    )


async def reject_unauthorized(update: Update):
    if update.effective_chat:
        logger.warning("Rejected command from unauthorized chat %s", update.effective_chat.id)

    if update.message:
        await update.message.reply_text("This bot is private.")


async def check_availability():
    """Checks adhahi.dz and returns (available, unavailable) wilaya lists."""
    try:
        return await asyncio.wait_for(
            _check_availability(),
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise AvailabilityCheckError(
            f"The website check timed out after {CHECK_TIMEOUT_SECONDS} seconds."
        ) from exc


async def run_availability_check(source: str):
    """Run one availability check while tracking who owns the shared lock."""
    global availability_check_started_at, availability_check_source

    async with availability_lock:
        availability_check_started_at = time.monotonic()
        availability_check_source = source
        try:
            return await check_availability()
        finally:
            availability_check_started_at = None
            availability_check_source = None


def current_check_description() -> str:
    if not availability_lock.locked():
        return "No website check is running right now."

    elapsed = 0
    if availability_check_started_at is not None:
        elapsed = int(time.monotonic() - availability_check_started_at)

    source = availability_check_source or "unknown"
    return (
        f"A {source} website check has been running for {elapsed} seconds. "
        f"The maximum check time is {CHECK_TIMEOUT_SECONDS} seconds."
    )


async def _check_availability():
    logger.info("Checking availability on %s...", URL)

    try:
        return await asyncio.to_thread(_check_availability_api)
    except AvailabilityCheckError as e:
        if not env_flag(BROWSER_FALLBACK_ENABLED):
            raise

        logger.warning("API availability check failed; falling back to browser: %s", e)

    return await _check_availability_browser()


def _check_availability_api():
    logger.info(
        "Checking availability API using %d endpoint(s)...",
        len(WILAYA_QUOTAS_API_URLS),
    )

    headers = {
        "Accept": "application/json",
        "Accept-Language": "ar-DZ,ar;q=0.9,fr-DZ;q=0.8,fr;q=0.7,en;q=0.6",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    errors: list[str] = []
    resolver_context = force_ipv4_requests() if env_flag(API_FORCE_IPV4) else nullcontext()

    with requests.Session() as session:
        with resolver_context:
            for url in WILAYA_QUOTAS_API_URLS:
                for attempt in range(1, API_RETRIES + 1):
                    try:
                        logger.info(
                            "Calling wilaya quotas API %s, attempt %d/%d...",
                            url,
                            attempt,
                            API_RETRIES,
                        )
                        response = session.get(
                            url,
                            headers=headers,
                            timeout=(
                                API_CONNECT_TIMEOUT_SECONDS,
                                API_TIMEOUT_SECONDS,
                            ),
                        )
                    except requests.RequestException as exc:
                        errors.append(f"{url}: {exc}")
                        logger.warning(
                            "Wilaya quotas API attempt failed for %s (%d/%d): %s",
                            url,
                            attempt,
                            API_RETRIES,
                            exc,
                        )
                        if attempt < API_RETRIES:
                            time.sleep(API_RETRY_DELAY_SECONDS)
                        continue

                    if response.status_code >= 400:
                        errors.append(f"{url}: HTTP {response.status_code}")
                        logger.warning(
                            "Wilaya quotas API returned HTTP %d for %s.",
                            response.status_code,
                            url,
                        )
                        break

                    break
                else:
                    continue

                if response.status_code < 400:
                    break
            else:
                details = " | ".join(errors[-4:]) if errors else "No API endpoints were configured."
                raise AvailabilityCheckError(
                    "Could not reach the wilaya quotas API. "
                    f"Tried {len(WILAYA_QUOTAS_API_URLS)} endpoint(s) with IPv4 "
                    f"{'enabled' if env_flag(API_FORCE_IPV4) else 'disabled'}. "
                    f"Last errors: {details}"
                )

    try:
        data = response.json()
    except ValueError as exc:
        raise AvailabilityCheckError("The wilaya quotas API did not return valid JSON.") from exc

    if not isinstance(data, list):
        raise AvailabilityCheckError("The wilaya quotas API returned an unexpected response.")

    available_wilayas: list[str] = []
    unavailable_wilayas: list[str] = []

    for item in data:
        if not isinstance(item, dict):
            continue

        wilaya_name = format_wilaya_quota(item)
        if not wilaya_name:
            continue

        if quota_item_is_available(item):
            available_wilayas.append(wilaya_name)
        else:
            unavailable_wilayas.append(wilaya_name)

    if not available_wilayas and not unavailable_wilayas:
        raise AvailabilityCheckError("The wilaya quotas API returned no usable wilayas.")

    logger.info(
        "Availability API returned %d available and %d unavailable wilayas.",
        len(available_wilayas),
        len(unavailable_wilayas),
    )
    return available_wilayas, unavailable_wilayas


def quota_item_is_available(item: dict) -> bool:
    if isinstance(item.get("available"), bool):
        return bool(item["available"])

    remaining_quota = item.get("remainingQuota")
    if remaining_quota is None:
        return False

    try:
        return int(remaining_quota) > 0
    except (TypeError, ValueError):
        return False


def format_wilaya_quota(item: dict) -> str:
    code = str(item.get("wilayaCode") or item.get("code") or "").strip()
    name = str(
        item.get("wilayaNameAr")
        or item.get("nameAr")
        or item.get("wilayaNameFr")
        or item.get("name")
        or ""
    ).strip()

    if not name and not code:
        return ""

    if code:
        digits = re.sub(r"\D", "", code)
        if digits:
            code = f"{int(digits):02d}"
        return f"{code} - {name or code}"

    return name


async def _check_availability_browser():
    logger.info("Checking availability with browser fallback on %s...", URL)

    available_wilayas: list[str] = []
    unavailable_wilayas: list[str] = []
    wilaya_input_selector = "#reg-wilaya"

    async with async_playwright() as p:
        launch_options = {
            "headless": True,
            "args": [
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--no-sandbox",
            ],
        }
        if CHROMIUM_EXECUTABLE_PATH:
            launch_options["executable_path"] = CHROMIUM_EXECUTABLE_PATH

        browser = await p.chromium.launch(**launch_options)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ar-DZ",
            timezone_id="Africa/Algiers",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "ar-DZ,ar;q=0.9,fr-DZ;q=0.8,fr;q=0.7,en;q=0.6",
            },
        )
        page = await context.new_page()
        page.set_default_timeout(SELECTOR_TIMEOUT_SECONDS * 1000)
        page.set_default_navigation_timeout(GOTO_TIMEOUT_SECONDS * 1000)

        async def block_heavy_resources(route):
            request = route.request
            if request.resource_type in {"image", "media", "font"}:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_heavy_resources)

        try:
            last_navigation_error: Exception | None = None
            for attempt in range(1, NAVIGATION_RETRIES + 1):
                try:
                    logger.info(
                        "Opening target page, attempt %d/%d...",
                        attempt,
                        NAVIGATION_RETRIES,
                    )
                    await page.goto(
                        URL,
                        wait_until="commit",
                        timeout=GOTO_TIMEOUT_SECONDS * 1000,
                    )

                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except PlaywrightTimeoutError:
                        logger.warning(
                            "Page did not reach domcontentloaded quickly; continuing with selector wait."
                        )

                    await page.wait_for_selector(
                        wilaya_input_selector,
                        state="visible",
                        timeout=SELECTOR_TIMEOUT_SECONDS * 1000,
                    )
                    break
                except Exception as e:
                    last_navigation_error = e
                    logger.warning(
                        "Page open attempt %d/%d failed: %s",
                        attempt,
                        NAVIGATION_RETRIES,
                        e,
                    )
                    if attempt < NAVIGATION_RETRIES:
                        await page.wait_for_timeout(2000 * attempt)
            else:
                raise AvailabilityCheckError(
                    "Could not open the registration page after "
                    f"{NAVIGATION_RETRIES} attempts. Last error: {last_navigation_error}"
                )

            page_text = await page.locator("body").inner_text(timeout=10000)
            if "Web Page Blocked" in page_text:
                raise AvailabilityCheckError("The website returned a block page.")

            await page.click(wilaya_input_selector)
            await page.wait_for_timeout(1000)

            list_items = await page.locator("ul[role='listbox'] li").all()

            if not list_items:
                logger.warning(
                    "Could not find the list of wilayas. The page structure might have changed."
                )
                raise AvailabilityCheckError(
                    "Could not find the wilaya list. The page may have changed."
                )

            wilaya_counter = 0
            for item in list_items:
                text = (await item.text_content()).strip()
                if not text:
                    continue
                wilaya_counter += 1

                class_name = (await item.get_attribute("class")) or ""

                # 'حجز غير متوفر حاليًا' means 'Reservation currently not available'
                is_unavailable_by_text = "غير متوفر" in text
                is_unavailable_by_class = "cursor-not-allowed" in class_name

                # Clean up the name
                wilaya_name = text.replace("— حجز غير متوفر حاليًا", "").strip()
                if wilaya_name and not wilaya_name[0].isdigit():
                    wilaya_name = f"{wilaya_counter:02d} - {wilaya_name}"

                # Treat it as unavailable only when both known disabled signals agree.
                # If either the text or CSS signal changes, count it as available.
                if is_unavailable_by_text and is_unavailable_by_class:
                    unavailable_wilayas.append(wilaya_name)
                else:
                    available_wilayas.append(wilaya_name)

        except Exception as e:
            logger.error("Error while checking page: %s", e)
            if ERROR_SCREENSHOT_PATH:
                try:
                    await page.screenshot(path=ERROR_SCREENSHOT_PATH)
                    logger.info("Saved error screenshot to %s", ERROR_SCREENSHOT_PATH)
                except Exception:
                    pass
            if isinstance(e, AvailabilityCheckError):
                raise
            raise AvailabilityCheckError(str(e)) from e
        finally:
            await browser.close()

    return available_wilayas, unavailable_wilayas


# ---------------------------------------------------------------------------
# Scheduled job (runs every 10 minutes automatically)
# ---------------------------------------------------------------------------

async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job that checks availability and notifies on changes."""
    global notified_wilayas

    if availability_lock.locked():
        logger.info("Skipping scheduled check because another check is already running.")
        return

    try:
        available_wilayas, _unavailable = await run_availability_check("scheduled")
    except AvailabilityCheckError as e:
        logger.error("Scheduled availability check failed: %s", e)
        return

    if available_wilayas:
        logger.info("Found %d available wilayas!", len(available_wilayas))

        new_wilayas = [w for w in available_wilayas if w not in notified_wilayas]

        if new_wilayas:
            wilayas_text = "\n".join([f"✅ {w}" for w in new_wilayas])
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            message = (
                f"🚨 <b>Available Wilayas Detected!</b>\n\n"
                f"Registration is <b>OPEN</b> for:\n\n"
                f"{wilayas_text}\n\n"
                f"🕒 <b>Time:</b> {current_time}\n"
                f"🔗 <b>Link:</b> <a href='{URL}'>Register Here</a>"
            )

            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
            )

            for w in new_wilayas:
                notified_wilayas.add(w)
        else:
            logger.info("Available wilayas already notified.")
    else:
        logger.info("No wilayas are currently available.")
        if notified_wilayas:
            logger.info("Clearing notified cache since all wilayas are unavailable.")
            notified_wilayas.clear()


# ---------------------------------------------------------------------------
# Bot command handlers
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /start — shows help."""
    if not is_authorized(update):
        await reject_unauthorized(update)
        return

    message = (
        "🐑 <b>Adhahi.dz Monitor Bot</b>\n\n"
        "I monitor the Adhahi registration page and alert you when wilayas become available.\n\n"
        "<b>Commands:</b>\n"
        "/check — Check availability right now\n"
        "/status — Show monitor status\n\n"
        f"📡 Auto-checking every <b>{CHECK_INTERVAL_SECONDS // 60} minutes</b>."
    )
    await update.message.reply_text(message, parse_mode="HTML")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /check — runs an immediate availability check."""
    if not is_authorized(update):
        await reject_unauthorized(update)
        return

    if availability_lock.locked():
        await update.message.reply_text(
            f"⏳ {current_check_description()}\n\nPlease try again shortly.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text("🔍 Checking availability now… please wait.")

    try:
        available_wilayas, unavailable_wilayas = await run_availability_check("manual")
    except AvailabilityCheckError as e:
        logger.error("Manual availability check failed: %s", e)
        await update.message.reply_text(
            "⚠️ <b>Could not complete the website check.</b>\n\n"
            f"{escape(str(e))}\n\n"
            "The bot is still running, but adhahi.dz may be blocking or delaying the availability request.",
            parse_mode="HTML",
        )
        return

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if available_wilayas:
        wilayas_text = "\n".join([f"✅ {w}" for w in available_wilayas])
        message = (
            f"🟢 <b>Available Wilayas ({len(available_wilayas)}):</b>\n\n"
            f"{wilayas_text}\n\n"
            f"🕒 <b>Checked at:</b> {current_time}\n"
            f"🔗 <b>Link:</b> <a href='{URL}'>Register Here</a>"
        )
    else:
        message = (
            f"🔴 <b>No wilayas available right now.</b>\n\n"
            f"All {len(unavailable_wilayas)} wilayas are currently unavailable.\n\n"
            f"🕒 <b>Checked at:</b> {current_time}\n"
            f"📡 Auto-monitoring is still active (every {CHECK_INTERVAL_SECONDS // 60} min)."
        )

    await update.message.reply_text(message, parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /status — shows monitor status."""
    if not is_authorized(update):
        await reject_unauthorized(update)
        return

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    notified_count = len(notified_wilayas)
    current_check = current_check_description()

    if notified_wilayas:
        notified_list = "\n".join([f"  • {w}" for w in sorted(notified_wilayas)])
        notified_section = f"\n\n<b>Currently notified wilayas:</b>\n{notified_list}"
    else:
        notified_section = ""

    message = (
        f"📊 <b>Monitor Status</b>\n\n"
        f"🟢 <b>Status:</b> Running\n"
        f"⏱️ <b>Interval:</b> Every {CHECK_INTERVAL_SECONDS // 60} minutes\n"
        f"🔎 <b>Current check:</b> {escape(current_check)}\n"
        f"🔔 <b>Notified wilayas:</b> {notified_count}"
        f"{notified_section}\n\n"
        f"🕒 <b>Current time:</b> {current_time}\n"
        f"🌐 <b>Target:</b> {URL}"
    )
    await update.message.reply_text(message, parse_mode="HTML")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Keep Telegram polling conflicts readable in hosted logs."""
    error = context.error
    if isinstance(error, Conflict):
        logger.error(
            "Telegram polling conflict detected. Another deployment or local process is already using this bot token. "
            "The bot will keep retrying; stop the duplicate process/service if this continues."
        )
        return

    logger.exception("Unhandled bot error", exc_info=error)


# ---------------------------------------------------------------------------
# Bot setup and startup
# ---------------------------------------------------------------------------

async def post_init(application: Application):
    """Called after the bot is initialized — sets commands menu and sends startup message."""
    try:
        await application.bot.set_my_commands(
            [
                BotCommand("check", "Check availability now"),
                BotCommand("status", "Show monitor status"),
                BotCommand("start", "Show help"),
            ]
        )

        startup_msg = (
            f"🟢 <b>Adhahi Monitor Started</b>\n"
            f"Checking <code>{URL}</code> every {CHECK_INTERVAL_SECONDS // 60} minutes.\n\n"
            f"Use /check to check availability at any time."
        )
        await application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=startup_msg,
            parse_mode="HTML",
        )
    except Forbidden as e:
        logger.error(
            "Telegram refused the startup message: %s. Check that TELEGRAM_CHAT_ID is the numeric recipient chat id, "
            "the recipient has started a chat with the bot, or the bot has permission in the target group.",
            e,
        )
        raise
    except TelegramError as e:
        logger.error("Telegram startup check failed: %s", e)
        raise


def main():
    logger.info("Starting Adhahi.dz Monitor Bot...")

    try:
        validate_configuration()
    except ConfigurationError as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)

    start_health_server()

    # Build the bot application
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_error_handler(error_handler)

    # Give manual checks room right after deploy before the first scheduled run.
    job_queue = application.job_queue
    job_queue.run_repeating(
        scheduled_check,
        interval=CHECK_INTERVAL_SECONDS,
        first=FIRST_CHECK_DELAY_SECONDS,
        job_kwargs={"max_instances": 1},
    )

    logger.info(
        "Bot is running. Auto-check every %d minutes. First scheduled check in %d seconds.",
        CHECK_INTERVAL_SECONDS // 60,
        FIRST_CHECK_DELAY_SECONDS,
    )

    # Start polling for commands — this blocks and runs the event loop
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
