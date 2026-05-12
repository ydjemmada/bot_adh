import os
import asyncio
import logging
import re
import sys
import threading
import time
from html import escape
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from telegram import Update, BotCommand
from telegram.error import Forbidden, TelegramError
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
    """Uses Playwright to check adhahi.dz and returns (available, unavailable) wilaya lists."""
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
            "The bot is still running, but adhahi.dz may be blocking or delaying the browser request.",
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

    # Give manual checks room right after deploy before the first scheduled run.
    job_queue = application.job_queue
    job_queue.run_repeating(
        scheduled_check,
        interval=CHECK_INTERVAL_SECONDS,
        first=FIRST_CHECK_DELAY_SECONDS,
        job_kwargs={"max_instances": 1},
    )

    logger.info(
        "Bot is running. Auto-check every %d minutes.", CHECK_INTERVAL_SECONDS // 60
    )

    # Start polling for commands — this blocks and runs the event loop
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
