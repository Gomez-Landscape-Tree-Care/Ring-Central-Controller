from datetime import datetime
from zoneinfo import ZoneInfo

from logger_config import logger
from config import MONDAY_USERS

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

def _sender(author_id: str) -> str:
    return MONDAY_USERS.get(author_id, {}).get('informal_name', 'Someone')

def determine_timeline_entry(message_type: str, author_id: str) -> str:
    if message_type == 'human':
        return sms_timeline_entry(author_id=author_id)
    if message_type == 'send_follow_up':
        return follow_up_timeline_entry(author_id=author_id)
    if message_type == 'send_follow_up_2':
        return follow_up_two_timeline_entry(author_id=author_id)
    if message_type == 'request_photos':
        return request_photos_timeline_entry(author_id=author_id)
    if message_type == 'send_job_application_english':
        return english_job_app_timeline_entry(author_id=author_id)
    if message_type == 'send_job_application_spanish':
        return spanish_job_app_timeline_entry(author_id=author_id)
    if message_type == 'invite':
        return send_invite_link_timeline_entry(author_id=author_id)
    if message_type == 'onboarding_follow_up':
        return onboarding_follow_up_timeline_entry()
    if message_type == 'interview_follow_up':
        return interview_follow_up_timeline_entry()
    if message_type == 'send_interview_link':
        return send_interview_link_timeline_entry(author_id=author_id)
    if message_type == 'send_onboarding_link':
        return send_onboarding_link_timeline_entry(author_id=author_id)
    if message_type == 'quote':
        return quote_timeline_entry()

    return ''

def sms_timeline_entry(author_id: str) -> str:
    """The Timeline line for an SMS sent off a board or a CL/AB request.

    An author MONDAY_USERS has no row for is named by nobody rather than by Jeff
    Bot: the fallback stands in for the name, not for the person, and crediting
    somebody else's text to the bot is worse than leaving it unattributed.
    """
    return f"{_sender(author_id=author_id)} sent a text message"

def forward_timeline_entry(author_id: str, recipient_names: list[str]) -> str:
    return f'{_sender(author_id=author_id)} forwarded to {', '.join(recipient_names)}'

def follow_up_timeline_entry(author_id: str) -> str:
    return f'Send follow up message by {_sender(author_id=author_id)}'

def follow_up_two_timeline_entry(author_id: str) -> str:
    return f'Send follow up message-2 by {_sender(author_id=author_id)}'

def request_photos_timeline_entry(author_id: str) -> str:
    return f'Request photos by {_sender(author_id=author_id)}'

def english_job_app_timeline_entry(author_id: str) -> str:
    return f'Send job application (English) by {_sender(author_id=author_id)}'

def spanish_job_app_timeline_entry(author_id: str) -> str:
    return f'Send job application (Spanish) by {_sender(author_id=author_id)}'

def send_invite_link_timeline_entry(author_id: str) -> str:
    return f'Send Invite Link by {_sender(author_id=author_id)}'

def onboarding_follow_up_timeline_entry() -> str:
    return 'Sent onboarding follow up message'

def interview_follow_up_timeline_entry() -> str:
    return 'Sent interview follow up message'

def send_interview_link_timeline_entry(author_id: str) -> str:
    return f'{_sender(author_id=author_id)} sent interview link'

def send_onboarding_link_timeline_entry(author_id: str) -> str:
    return f'{_sender(author_id=author_id)} sent onboarding link'

def quote_timeline_entry() -> str:
    return 'Client requested a quote'