"""scrapit - a polite, terminal-based website path fetcher."""

import logging

__version__ = "2.4.0"
__author__ = "S4rtx"
__all__ = ["__version__", "__author__"]

logging.getLogger("scrapit").addHandler(logging.NullHandler())
logging.getLogger("scrapit").propagate = False
logging.getLogger("scrapit.audit").addHandler(logging.NullHandler())
logging.getLogger("scrapit.audit").propagate = False
