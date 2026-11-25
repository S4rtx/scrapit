"""scrapit - a polite, terminal-based website path fetcher."""

import logging

__version__ = "2.2.0"
__all__ = ["__version__"]

logging.getLogger("scrapit").addHandler(logging.NullHandler())
logging.getLogger("scrapit").propagate = False
logging.getLogger("scrapit.audit").addHandler(logging.NullHandler())
logging.getLogger("scrapit.audit").propagate = False
