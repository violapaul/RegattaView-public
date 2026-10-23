"""
Quick copy of the logging boilerplate.
This module provides a standardized logging setup with console output and configurable log levels.
"""

import logging
from typing import TextIO

def setup_logger(level: int = logging.DEBUG, name: str = 'regatta_analysis') -> tuple[logging.Logger, logging.StreamHandler[TextIO]]:
    """
    Set up a logger with console handler and custom formatting.
    
    Args:
        level: The logging level (default: logging.DEBUG)
        name: The name of the logger (default: 'regatta_analysis')
        
    Returns:
        Tuple containing the logger and its console handler
    """
    # create logger
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # create console handler and set level to debug
    ch = logging.StreamHandler()
    ch.setLevel(level)

    # create formatter with line number
    formatter = logging.Formatter('%(asctime)s|%(levelname)s|%(funcName)s:%(lineno)d| %(message)s')
    # add formatter to ch
    ch.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(ch)
    return logger, ch

class Logger:
    """
    A class that holds the logger instance and its handler for easy access and configuration.
    """
    
    def __init__(self, level: int) -> None:
        """
        Initialize the Logger with a specified logging level.
        
        Args:
            level: The initial logging level
        """
        logger, handler = setup_logger(level=level)
        self.logger: logging.Logger = logger
        self.handler: logging.StreamHandler[TextIO] = handler

    def set_logging_level(self, level: str | int) -> None:
        """
        Set the logging level for both the logger and its handler.
        
        Args:
            level: The logging level as a string (e.g., 'DEBUG', 'INFO') or integer
            
        Raises:
            ValueError: If the provided string level is not a valid logging level
        """
        if isinstance(level, str):
            numeric_level = getattr(logging, level.upper(), None)
            if not isinstance(numeric_level, int):
                raise ValueError(f"Invalid log level: {level}")
            print(f"Setting log level to {level}")
            self.logger.setLevel(numeric_level)
            self.handler.setLevel(numeric_level)
        else:
            self.logger.setLevel(level)
            self.handler.setLevel(level)

# Create a global logger instance with INFO level
LOG = Logger(logging.INFO)

# Commented out constants for reference
# DEBUG = logging.DEBUG
# WARNING = logging.WARNING