"""Route inbound monday.com webhook payloads by trigger type."""

from datetime import datetime, timezone

from config import SELF_AUTHORED_UPDATE_MARKER
from logger_config import logger
from services.monday.controller import board_for_id
from services.monday.controller import get_controller as get_monday_controller
from services.ring_central.workflow import send_and_record

CREATE_UPDATE = "create_update"


def process_monday(payload):
    """Route one monday webhook by the trigger that fired it."""
    event = payload.get("event") or {}
    trigger = event.get("type")
    if trigger == CREATE_UPDATE:
        return process_create_update(event)

    logger.info("Ignoring monday webhook of type %s", trigger)
    return None


def process_create_update(event):
    """An agent typed an update on a board item, meaning text this to the person.

    Updates are the surface this service and the agents share - the SMS history
    record_sms posts and the text an agent wants sent out are both updates on the
    same item - so the marker update_body leaves is what tells them apart, and
    what keeps the updates send_and_record writes from texting themselves out.

    The gates below are the ones only a monday webhook can answer. The rollout
    gate, the send, and everything written after it are send_and_record's.
    """
    update_id = event.get("updateId")
    if SELF_AUTHORED_UPDATE_MARKER in (event.get("body") or ""):
        logger.info("Update %s is one this service wrote", update_id)
        return None

    # textBody rather than body: monday has already rendered the update's HTML
    # down to text, and stripping it here would be a second, worse copy of that.
    text = (event.get("textBody") or "").strip()
    if not text:
        logger.info("Update %s carries no text to send", update_id)
        return None

    board_id = event.get("boardId")
    board = board_for_id(board_id)
    if not board:
        logger.info("Update %s is on board %s, which this service does not text from",
                    update_id, board_id)
        return None

    item_id = str(event.get("pulseId") or event.get("itemId") or "")
    monday = get_monday_controller()
    phone = monday.item_phone(board, item_id)
    if not phone:
        return None

    # The agent's own item already carries what they typed, so it is the one
    # board send_and_record leaves alone.
    return send_and_record(
        phone, text,
        timestamp=trigger_time(event),
        author_id=str(event.get("userId") or ""),
        op=f"Text update {update_id} to {phone}",
        skip_board=board,
        message_type='human')


def trigger_time(event) -> datetime:
    """When monday says the update was typed, falling back to now.

    Aware, always: this stamp is what render_timeline prints and what Slash
    orders the column by, and a naive one is read as the Lambda's own clock - UTC
    deployed, Pacific on a laptop, so the same entry would render two times.
    """
    raw = event.get("triggerTime")
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        logger.warning("Update %s has no usable triggerTime (%r)",
                       event.get("updateId"), raw)
        return datetime.now(timezone.utc)
