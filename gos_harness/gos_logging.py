"""
The module encapsulates logging and printing in the Graal Load Tester
"""
from __future__ import annotations

import logging
import pprint
import shutil
import sys
import time
import traceback
from collections.abc import Sequence
from typing import Any, cast

LAST_TIMESTAMP = time.time()
logger = logging.getLogger("gos_logger")


class _LiveStdoutHandler(logging.StreamHandler):
    """Stream handler that always writes to the current stdout.

    Pytest and CI log capture can replace and close `sys.stdout` between phases.
    Binding the handler to a specific stdout object can then lead to writes to a
    closed stream. Refreshing the stream on each emit avoids holding a stale
    reference.
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def _mark_owned_handler(handler: logging.Handler) -> logging.Handler:
    setattr(handler, "_gos_owned_handler", True)
    return handler


class MemoryLogHandler(logging.Handler):
    """Logging handler that stores formatted log lines in memory."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self._messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self._messages.append(self.format(record))

    def content(self) -> str:
        return "\n".join(self._messages)


def attach_memory_log_handler() -> MemoryLogHandler:
    """Attach and return an in-memory log handler for the current run."""
    handler = cast(MemoryLogHandler, _mark_owned_handler(MemoryLogHandler()))
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return handler


def _clear_owned_handlers() -> None:
    """Remove and close handlers previously created by setup_logger()."""
    for handler in logger.handlers[:]:
        if getattr(handler, "_gos_owned_handler", False):
            logger.removeHandler(handler)
            handler.close()


def setup_logger(loggers: str, log_level: str = "DEBUG") -> None:
    """Sets the logging according to the list of given loggers

    Loggers are in the format of logger1,logger2,...

    Currently, following loggers are supported:
      1. stdout -> prints to stdout (d'oh)
      2. file:<PATH> -> prints to file specified by path

    :param loggers: list of logges separated by ,
    :param log_level: log level of the logger, by default "DEBUG"
    """
    global logger
    logger.setLevel(getattr(logging, log_level, logging.DEBUG))
    _clear_owned_handlers()

    for logger_type in loggers.split(","):
        if logger_type == 'stdout':
            stdout_handler = _mark_owned_handler(_LiveStdoutHandler())
            stdout_handler.setLevel(logging.DEBUG)
            logger.addHandler(stdout_handler)
        elif logger_type.startswith('file'):
            log_file = logger_type.split(":", maxsplit=1)[1]
            file_handler = _mark_owned_handler(logging.FileHandler(log_file, mode="w"))
            file_handler.setLevel(logging.DEBUG)
            logger.addHandler(file_handler)
        else:
            print(f"Error while initializing loggers: unknown logger {logger_type}")
            print(f" - choose one of ('stdout', 'file:<PATH>')")
            sys.exit(1)


def is_logger_level_enough(level_str: str) -> bool:
    """Tests if the logging is at least level_str"""
    level = getattr(logging, level_str.upper(), None)
    return level is not None and logger.getEffectiveLevel() <= level


def log_exceptions(exceptions: Sequence[BaseException], log_non_debug: bool = True) -> None:
    """Logs exception details based on the configured logger level.

    In DEBUG, full tracebacks are printed. Otherwise, only concise messages
    are logged when log_non_debug is enabled.

    :param exceptions: list of exceptions
    :param log_non_debug: whether to log concise messages below DEBUG level
    """
    if is_logger_level_enough("DEBUG"):
        for err in exceptions:
            logger.info(f"--- {err.__class__.__name__} ---")
            logger.info("".join(traceback.format_tb(err.__traceback__)))
            logger.info(err)
    elif log_non_debug:
        for exception in exceptions:
            log(f" - {exception}")


def print_exceptions(exceptions: Sequence[BaseException], log_non_debug: bool = True) -> None:
    """Compatibility wrapper for log_exceptions()."""
    log_exceptions(exceptions, log_non_debug=log_non_debug)


def log_exception(exception: BaseException, log_non_debug: bool = True) -> None:
    """Logs exception details based on the configured logger level.

    In DEBUG, the full traceback is printed. Otherwise, only a concise message
    is logged when log_non_debug is enabled.

    :param exception: exception to log
    :param log_non_debug: whether to log a concise message below DEBUG level
    """
    log_exceptions([exception], log_non_debug=log_non_debug)

def format_elapsed(seconds: float) -> str:
    """Formats the seconds as follows:

    For seconds < 60:   (+SS.MMMs)
    For seconds < 3600: (+MM:SS.MMMm)
    Otherwise:          (+HH:MM:SS.MMMh)

    :param seconds: elapsed number of seconds
    """
    if seconds < 60:
        return f" (+{seconds:06.3f}s)"
    elif seconds < 3600:
        minutes, seconds = divmod(seconds, 60)
        return f" (+{int(minutes):02d}:{seconds:06.3f}m)"
    else:
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f" (+{int(hours):02d}:{int(minutes):02d}{seconds:06.3f}h)"

def log(message: object, no_elapsed: bool = False) -> None:
    """Generic wrapper for any logging and printing done inside the Graal Load Tester

    It prints the timestamps and how many seconds/minutes/hours has passed from previous timestamp (unless `no_elapsed` is set to true)

    :param no_elapsed: if set, the output will not print the time elapsed from previous captured timestamp
    """
    global LAST_TIMESTAMP
    now = time.time()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    milliseconds = int((now % 1) * 1000)
    if no_elapsed:
        diff = ""
    else:
        diff = format_elapsed(now - LAST_TIMESTAMP) if LAST_TIMESTAMP else ""
        LAST_TIMESTAMP = now

    logger.info(f"{timestamp}.{milliseconds:03d}{diff} {message}")

def raw_log(message: object) -> None:
    """Logs the message without any timestamps and elapsed times"""
    logger.info(message)

def header(header: str) -> None:
    """Prints the given string in formatted matter to show a significant phase in the load tesing

    --------------
    === header ===
    --------------
    
    :param header: string print inside the header
    """
    terminal_size = shutil.get_terminal_size()
    logger.info('-'*terminal_size.columns)
    logger.info(f"=== {header.center(terminal_size.columns - 8)} ===")
    logger.info('-'*terminal_size.columns)

def log_file_content(filename: str, content: str) -> None:
    """Prints the filename and its content in formatted manner

    ┌────────────────┐
        filename
    ├────────────────┤
    ....
    └────────────────┘

    """
    terminal_size = shutil.get_terminal_size()
    filename_len = len(filename)
    border_width = terminal_size.columns - 2
    logger.info('┌' + '─' * border_width + '┐')
    logger.info(f"{filename.center(max(filename_len, terminal_size.columns))}")
    logger.info('├' + '─' * border_width + '┤')
    logger.info(content)
    logger.info('└' + '─' * border_width + '┘')

def die(reason: str) -> None:
    """Graceful death with msg"""
    log(f"[ERROR] {reason}")
    sys.exit(1)

def log_pprint(*args: Any, **kwargs: Any) -> None:
    """Wrapper of the pretty print using with the logger
    
    :param args: positional args forwarded to pprint.pformat
    :apram kwargs: keyword args forwarded to pprint.pformat
    """
    logger.info(pprint.pformat(*args, **kwargs))
