import logging
from datetime import datetime
from zoneinfo import ZoneInfo

LA = ZoneInfo("America/Los_Angeles")


class _LAFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        """Format the log record's creation time in America/Los_Angeles with milliseconds.

        Overrides the default UTC-based formatter. The output format is
        `MM/DD/YY HH.MM.SS.mmmam` — dots instead of colons so the timestamp is
        copy-pasteable without escaping in most shells.

        Args:
            record: The log record whose `created` timestamp (Unix float) is formatted.
            datefmt: Ignored — this formatter always uses its own format string.
        """
        local = datetime.fromtimestamp(record.created, tz=LA)
        return local.strftime(datefmt or "%Y-%m-%d %H:%M:%S %Z")



logger = logging.getLogger()
logger.setLevel(logging.INFO)
for h in logger.handlers:
    h.setFormatter(_LAFormatter("%(asctime)s %(levelname)s %(message)s"))