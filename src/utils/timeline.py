from datetime import datetime
from zoneinfo import ZoneInfo

from logger_config import logger

_DIVIDER = "-" * 40
_PACIFIC = ZoneInfo("America/Los_Angeles")
_MAX_COLUMN_CHARS = 2000

def make_timeline_entry(message: str, dt: datetime | None = None) -> str:
    """Build a formatted timeline entry line.

    Args:
        message: The human-readable event description (e.g. "Vig answered").
        dt: The event time. Converted to Pacific before formatting. Defaults to now.

    Returns:
        A string like "6/19/26 9.05am - Vig answered".
    """
    if dt is None:
        dt = datetime.now(tz=_PACIFIC)
    local = dt.astimezone(_PACIFIC)
    stamp = local.strftime("%-m/%-d/%y %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    return f"{stamp} - {message}"

def _separator(prev_date, date) -> str:
    """The text between two adjacent entries: a divider across a day boundary, else a blank line.

    Either date may be None (an unparseable stamp), which collapses to the blank line.
    """
    if prev_date and date and prev_date != date:
        return f"\n{_DIVIDER}\n"
    return "\n\n"


def render_timeline(rows: list[tuple[str, datetime]]) -> str:
    """Build the full column text from `timelines` rows, newest first.

    The rebuild counterpart to `prepend_timeline`: same stamp, same separators, applied to
    every row at once instead of one new entry against an existing blob.

    Capped at `_MAX_COLUMN_CHARS`: entries are taken newest first and the first one that
    would push the text past the cap is dropped, along with every older entry behind it. The
    cut lands on an entry boundary, never mid-line. The rows themselves are untouched — the
    `timelines` table stays the full history, and the column shows as much of its recent end
    as fits.

    Args:
        rows: `(entry, timestamp)` pairs, already ordered newest first. Entries are the raw
              unstamped messages the table stores; the stamp comes from `timestamp`.

    Returns:
        The assembled column text. Empty only when there are no rows — a single newest entry
        over the cap on its own is kept, hard-truncated, rather than rendering nothing and
        blanking the column.
    """
    parts: list[str] = []
    length = 0
    prev_date = None
    for i, (entry, timestamp) in enumerate(rows):
        date = timestamp.astimezone(_PACIFIC).date()
        chunk = make_timeline_entry(entry, timestamp)
        if prev_date is not None:
            chunk = _separator(prev_date, date) + chunk
        if length + len(chunk) > _MAX_COLUMN_CHARS:
            if parts:
                logger.info(
                    f"[timeline] {_MAX_COLUMN_CHARS}-char cap reached after {i} entries, "
                    f"{len(rows) - i} older dropped"
                )
                break
            # The newest entry alone is over the cap. Keep it, cut to fit, rather than
            # rendering nothing — a rebuild replaces the whole column, so an empty render
            # would blank it.
            logger.warning(f"[timeline] newest entry exceeds {_MAX_COLUMN_CHARS} chars, truncated")
            parts.append(chunk[:_MAX_COLUMN_CHARS])
            break
        parts.append(chunk)
        length += len(chunk)
        prev_date = date
    return "".join(parts).strip()