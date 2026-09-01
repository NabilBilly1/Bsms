import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Create logs directory if it doesn't exist
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "classhouse.log"

# Define log format
LOG_FORMAT = (
    "%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
)


def setup_logging():
    """
    Configures centralized logging for the application.
    Logs to both console and a rotating file.
    """
    # Create root logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Remove existing handlers to avoid duplicates if re-initialized
    if logger.hasHandlers():
        logger.handlers.clear()

    # Create formatters
    formatter = logging.Formatter(LOG_FORMAT)

    # Console Handler (Stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    # Rotating File Handler (Max 10MB per file, keeps 5 backups)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    # Add handlers to root logger
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    # Quiet down some overly verbose third-party libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("pika").setLevel(logging.WARNING)

    return logger


# Initialize logger
logger = setup_logging()


def get_logger(name: str):
    """
    Returns a logger instance with a specific name.
    """
    return logging.getLogger(name)
