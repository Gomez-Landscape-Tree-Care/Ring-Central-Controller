"""Route inbound monday.com webhook payloads by trigger type."""

from datetime import datetime, timezone

from config import MONDAY_USERS, SELF_AUTHORED_UPDATE_MARKER, TEST_PHONE
from logger_config import logger
from services.monday.controller import board_for_id
from services.monday.controller import get_controller as get_monday_controller
from services.ring_central.controller import RingCentralController
from services.ring_central.model import MAX_SMS_CHARS
from services.ring_central.workflow import (SMS_ENTRY_TYPE, rebuild_timelines,
                                            update_body)
from services.slash.controller import get_controller as get_slash_controller

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

    The outbound half of record_sms, and its mirror image in what may fail. An
    SMS is the one thing this service does that cannot be taken back, and the
    webhook arrives over a FIFO queue that redelivers whatever raises - so
    everything that can fail is done before the send and nothing after it is
    allowed to raise. That ordering is the at-most-once guarantee, not a
    tidiness: a raise past send_sms would put a second copy of an agent's text
    on a real person's phone.

    Updates are the surface this service and the agents share - the SMS history
    record_sms posts and the text an agent wants sent out are both updates on the
    same item - so the marker update_body leaves is what tells them apart, and
    what keeps the mirror below from texting itself back out.
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

    # Cut here rather than leaving it to the model's own slice, so the Slash
    # record and the mirrored update carry what went on the wire rather than what
    # the agent typed.
    if len(text) > MAX_SMS_CHARS:
        logger.warning("Update %s is %d chars, sending the first %d",
                       update_id, len(text), MAX_SMS_CHARS)
        text = text[:MAX_SMS_CHARS]

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

    # The rollout gate the others cannot stand in for: resolve_items and the
    # Slash controller each hold one, but neither is on the send path, and this
    # is the only handler where a number outside the rollout would be texted
    # rather than merely written about.
    if phone != TEST_PHONE:
        logger.info("%s is outside the rollout, leaving update %s unsent",
                    phone, update_id)
        return None

    # Up front, once, as record_sms resolves before its writes: the rebuild and
    # the mirror below write to the same items, and this is the board search they
    # would otherwise pay for separately.
    items = monday.resolve_items(phone)
    timestamp = trigger_time(event)

    RingCentralController().send_sms(
        text=text, to_number=phone,
        op=f"Text update {update_id} to {phone}")
    
    author_id = str(event.get("userId") or "")
    slash = get_slash_controller()
    try:
        slash.create_text_message(
            phone=phone, text=text, timestamp=timestamp, outbound=True,
            monday_user_id=author_id)
    except Exception:
        logger.exception("Slash message failed for update %s, already sent", update_id)

    try:
        slash.create_timeline_entry(
            phone=phone,
            entry=timeline_entry(author_id),
            entry_type=SMS_ENTRY_TYPE,
            timestamp=timestamp,
            outbound=True)
    except Exception:
        logger.exception("Slash timeline entry failed for update %s, already sent",
                         update_id)

    rebuild_timelines(phone, items)

    # The agent's own item already carries what they typed; the other board holds
    # the same person and would otherwise show the timeline line with no message
    # behind it. Skipped by board rather than by item id, because two items on one
    # board can carry the same phone and resolve_items answers with the first -
    # which need not be the one the agent typed on.
    body = update_body(text, timestamp, True)
    for other, other_item_id in items:
        if other.id != board.id:
            monday.create_update(other, other_item_id, body)
    return None


def timeline_entry(author_id) -> str:
    """The Timeline line for an SMS an agent sent off a board.

    An author MONDAY_USERS has no row for is named by nobody rather than by Jeff
    Bot: the fallback stands in for the name, not for the person, and crediting
    somebody else's text to the bot is worse than leaving it unattributed.
    """
    sender = MONDAY_USERS.get(author_id, {}).get('informal_name')
    return f"{sender} sent a text message" if sender else "Someone sent a text message"


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
