from collections.abc import Callable
from datetime import datetime

from contracts import utc_now

type Clock = Callable[[], datetime]

__all__ = ["Clock", "utc_now"]
