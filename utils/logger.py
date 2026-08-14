"""
Logging configuration for the trading project.

Features:
- Multi-level logging (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- Separate log files:
  - trader.log: Main application logs
  - error.log: Error-only logs
  - llm_calls.jsonl: Structured logs for all LLM API calls
- Console and file output
- Rotating file handlers (daily rotation)
- Configurable via config.py
"""
import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

# Import configuration from config.py
try:
    from core.config import LOG_DIR, MAX_LOG_SIZE_MB, LOG_BACKUP_COUNT, DEFAULT_LOG_LEVEL
except ImportError:
    # Fallback defaults if config not available
    LOG_DIR = Path(__file__).parent.parent / "logs"
    MAX_LOG_SIZE_MB = 10
    LOG_BACKUP_COUNT = 5
    DEFAULT_LOG_LEVEL = "INFO"


# ==================== Configuration ====================

_LOG_DIR = Path(LOG_DIR)
_LOG_DIR.mkdir(exist_ok=True)

# Log file paths
MAIN_LOG_FILE = _LOG_DIR / f"trader_{datetime.now().strftime('%Y%m%d')}.log"
ERROR_LOG_FILE = _LOG_DIR / f"errors_{datetime.now().strftime('%Y%m%d')}.log"
LLM_LOG_FILE = _LOG_DIR / "llm_calls.jsonl"

# Max size and backup count for rotating file handlers
MAX_BYTES = MAX_LOG_SIZE_MB * 1024 * 1024  # Convert MB to bytes
BACKUP_COUNT = LOG_BACKUP_COUNT

# Log formats
CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


# ==================== Global Logger Initialization ====================

_logger_initialized = False
main_logger = None
error_logger = None


def init_logger(
    level: Optional[int] = None,
    enable_console: bool = True,
    enable_file: bool = True,
):
    """Initialize global loggers
    
    Args:
        level: Minimum logging level (logging.INFO, logging.DEBUG, etc.)
            If None, use config.DEFAULT_LOG_LEVEL
        enable_console: Whether to output logs to console
        enable_file: Whether to write logs to files
    """
    global _logger_initialized, main_logger, error_logger
    
    if _logger_initialized:
        return
    
    # Convert string level to logging constant if not provided
    if level is None:
        level_map = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "CRITICAL": logging.CRITICAL,
        }
        level = level_map.get(DEFAULT_LOG_LEVEL.upper(), logging.INFO)
    
    _logger_initialized = True
    
    # Create main logger
    main_logger = logging.getLogger("trader")
    main_logger.setLevel(level)
    
    # Create error logger (only ERROR and above)
    error_logger = logging.getLogger("trader.error")
    error_logger.setLevel(logging.ERROR)
    
    if enable_console:
        _add_console_handler(main_logger, error_logger)
    
    if enable_file:
        _add_file_handlers(main_logger, error_logger)


def _add_console_handler(main_log: logging.Logger, error_log: logging.Logger):
    """Add console handler to loggers"""
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)  # Show everything on console
    console_formatter = logging.Formatter(CONSOLE_FORMAT, datefmt=DATE_FORMAT)
    console_handler.setFormatter(console_formatter)
    
    main_log.addHandler(console_handler)
    error_log.addHandler(console_handler)


def _add_file_handlers(main_log: logging.Logger, error_log: logging.Logger):
    """Add rotating file handlers to loggers"""
    # Main logger handlers
    main_file_handler = RotatingFileHandler(
        MAIN_LOG_FILE,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8"
    )
    main_file_handler.setLevel(logging.INFO)
    main_file_handler.setFormatter(logging.Formatter(FILE_FORMAT, datefmt=DATE_FORMAT))
    
    main_log.addHandler(main_file_handler)
    
    # Error logger handlers
    error_file_handler = TimedRotatingFileHandler(
        ERROR_LOG_FILE,
        when="midnight",
        interval=1,
        backupCount=BACKUP_COUNT,
        encoding="utf-8"
    )
    error_file_handler.setLevel(logging.ERROR)
    error_file_handler.setFormatter(logging.Formatter(FILE_FORMAT, datefmt=DATE_FORMAT))
    
    error_log.addHandler(error_file_handler)


# ==================== Helper Functions ====================

def get_logger(name: str = __name__) -> logging.Logger:
    """Get a logger instance for the calling module
    
    Usage:
        in module.py:
            logger = utils.logger.get_logger()  # name will be 'module'
            logger.info("Some message")
    """
    if not _logger_initialized:
        init_logger()
    
    return logging.getLogger(name)


def log_llm_call(
    provider: str,
    model: str,
    system_prompt: str,
    user_message: str,
    response: str,
    result: Dict[str, Any],
    error: Optional[str] = None,
):
    """Log an LLM API call to JSONL file
    
    This is useful for debugging/auditing LLM interactions.
    Every call gets logged with full context.
    
    Args:
        provider: API provider (glm/openai/deepseek)
        model: Model name used
        system_prompt: System prompt content
        user_message: User message content
        response: Raw response text from API
        result: Parsed result dict
        error: Error message if any
    """
    record = {
        "timestamp": datetime.now().isoformat(),
        "provider": provider,
        "model": model,
        "system_prompt": system_prompt,
        "user_message": user_message,
        "system_prompt_length": len(system_prompt),
        "user_message_length": len(user_message),
        "response_length": len(response) if response else 0,
        "result": result,
        "error": error,
    }
    
    try:
        with open(LLM_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Warning] Failed to write LLM log: {e}")


def clear_llm_log():
    """Clear LLM call log file"""
    if LLM_LOG_FILE.exists():
        LLM_LOG_FILE.unlink()
        logger = get_logger(__name__)
        logger.info(f"Cleared LLM log file: {LLM_LOG_FILE}")


# ==================== Convenience Logging Decorator ====================

def log_execution(func):
    """Decorator to log function entry/exit/errors
    
    Usage:
        @utils.logger.log_execution
        def my_function(arg1, arg2):
            pass
    """
    logger = get_logger(func.__module__)
    
    def wrapper(*args, **kwargs):
        logger.debug(f"Calling {func.__name__} with args={args}, kwargs={kwargs}")
        try:
            result = func(*args, **kwargs)
            logger.debug(f"{func.__name__} completed successfully")
            return result
        except Exception as e:
            logger.exception(f"{func.__name__} raised exception: {e}")
            raise
    
    return wrapper
