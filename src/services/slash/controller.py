"""The Slash backend's business-facing layer, one level up from slash/client.py.

The mapping from this codebase's vocabulary onto the backend's lives here and
nowhere else - the client speaks HTTP and knows nothing about an Application.
"""
from datetime import datetime, timezone
from functools import lru_cache
from dataclasses import dataclass

from logger_config import logger
from services.slash.model import SlashModel


@dataclass(frozen=True)
class SlashTimelineEntry:
    """One entry as FullTimeline's two formatters read it.

    Only the two fields they touch. entry_type and outbound come back on the
    wire too, but nothing on this side reads them, and a field no caller uses is
    a field that can go stale against the backend unnoticed.
    """

    entry: str
    timestamp: datetime

    @classmethod
    def from_api(cls, payload: dict) -> "SlashTimelineEntry":
        """One entry off the JSON the timeline route returns.

        The timestamp is parsed here rather than by the caller because
        internal_timestamp calls .astimezone(), which reads a naive datetime as
        the Lambda's own clock - UTC in Lambda, Pacific on a laptop, so the same
        entry would render two different times. A naive value is therefore
        stamped UTC, which is what the backend stores.
        """
        stamp = datetime.fromisoformat(payload["timestamp"])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return cls(entry=payload["entry"], timestamp=stamp)


class SlashController:
    """Business-facing API over `SlashClient` - the only class automation handlers use."""

    def __init__(self) -> None:
        self._model = SlashModel()

    def create_timeline_entry(self, phone: str, entry: str, entry_type: str,
                              timestamp: datetime, outbound: bool = True) -> None:
        """Keep one line of an applicant's Timeline column against the person it belongs to.

        The rollout gate lives here rather than at the seven call sites, which is
        what db.record.record_timeline_entry did before this took its place -
        seven copies of the same `if` is exactly what that function existed to
        avoid. upsert_person's gate stays at its call site; that is one site.

        Nothing is caught: a Slash that cannot be reached DLQs the message, the
        same contract upsert_person has. Every caller writes the entry before
        rebuilding the Timeline column from it, so a swallowed failure would put
        a line on the board that Slash does not have.
        """
        self._model.create_timeline_entry(
            phone=phone, entry=entry, entry_type=entry_type,
            timestamp=timestamp, outbound=outbound)
        logger.info("Recorded %s %s timeline entry for %s",
                    "outbound" if outbound else "inbound", entry_type, phone)
        return None

    def create_text_message(self, phone: str, text: str, timestamp: datetime,
                            outbound: bool, monday_user_id: str | None = None) -> None:
        """Keep one SMS, in whichever direction it went, against the person it belongs to."""
        self._model.create_text_message(
            phone=phone, text=text, outbound=outbound,
            monday_user_id=monday_user_id, timestamp=timestamp)
        logger.info("Recorded %s text for %s",
                    "outbound" if outbound else "inbound", phone)
        return None

    def timeline_entries(self, phone: str) -> list[SlashTimelineEntry]:
        """Every entry Slash holds for this applicant, newest first.

        The order comes off the wire and is not re-sorted here, which is the
        stronger guarantee rather than the lazier one: the backend orders by
        timestamp then id, both descending, and one workflow run can land
        several entries in the same second - a sort on timestamp alone would
        throw that tiebreak away. Order is load-bearing because render_timeline
        stops at the first line that overruns the column cap, so an out-of-order
        read does not fail, it silently drops the wrong entries.
        """
        entries = [SlashTimelineEntry.from_api(e)
                   for e in self._model.timeline_entries(phone=phone)]
        return entries


@lru_cache(maxsize=1)
def get_controller() -> SlashController:
    """The one controller a warm container uses, as ring_central.client.get_client is.

    The controller is where the client's session, bearer token and company id are
    cached, so building a new one per call throws all three away and pays an
    extra sign-in and GET /companies for every message.
    """
    return SlashController()