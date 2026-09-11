from datetime import datetime
from zoneinfo import ZoneInfo

def internal_timestamp(date: datetime | None=None) -> str:
    if date:
        return date.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%-m/%-d/%y %-I:%M%p").lower()
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%-m/%-d/%y %-I:%M%p").lower()