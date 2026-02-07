import asyncio
import logging
import os
from datetime import datetime, timedelta

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("twitch_bot")
logger.setLevel(logging.INFO)

_log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)

# Console handler
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)
logger.addHandler(_console_handler)

_file_handler = None


def _log_path_for_date(dt: datetime) -> str:
    return os.path.join(LOG_DIR, f"bot-{dt.strftime('%Y-%m-%d')}.log")


def setup_file_handler_for_today():
    """Set up a file handler for today's date, replacing the old one if needed."""
    global _file_handler

    if _file_handler is not None:
        logger.removeHandler(_file_handler)
        try:
            _file_handler.close()
        except Exception:
            pass

    path = _log_path_for_date(datetime.now())
    _file_handler = logging.FileHandler(path, encoding="utf-8")
    _file_handler.setFormatter(_log_formatter)
    logger.addHandler(_file_handler)
    logger.info("Log file handler set to %s", path)


def cleanup_old_logs(retention_days: int = 7):
    """Delete log files older than `retention_days` days."""
    try:
        today = datetime.now().date()
        cutoff = today - timedelta(days=retention_days)

        for filename in os.listdir(LOG_DIR):
            if not (filename.startswith("bot-") and filename.endswith(".log")):
                continue

            date_str = filename[len("bot-"):-len(".log")]
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            if file_date < cutoff:
                full_path = os.path.join(LOG_DIR, filename)
                try:
                    os.remove(full_path)
                    logger.info("Deleted old log file: %s", full_path)
                except Exception as e:
                    logger.error("Failed to delete old log file %s: %s", full_path, e)
    except Exception:
        logger.exception("Error during log cleanup")


async def log_maintenance_loop():
    """Rotate logs daily and clean up old files."""
    while True:
        try:
            now = datetime.now()
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=5, microsecond=0
            )
            sleep_seconds = (tomorrow - now).total_seconds()
            await asyncio.sleep(sleep_seconds)

            logger.info("Running daily log rotation & cleanup...")
            setup_file_handler_for_today()
            cleanup_old_logs(retention_days=7)
            logger.info("Log rotation & cleanup complete.")
        except asyncio.CancelledError:
            logger.info("Log maintenance loop cancelled.")
            break
        except Exception:
            logger.exception("Error in log maintenance loop")


# Initialize today's file handler immediately
setup_file_handler_for_today()
cleanup_old_logs(retention_days=7)
