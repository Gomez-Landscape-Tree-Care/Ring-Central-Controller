"""Route inbound RingCentral webhook notifications by event type."""

from datetime import datetime

from config import SELF_AUTHORED_UPDATE_MARKER
from logger_config import logger
from services.monday.controller import get_controller as get_monday_controller
from services.ring_central.controller import RingCentralController
from services.slash.controller import get_controller as get_slash_controller
from utils.date import internal_timestamp


def process_ring_central(event):
    """Route one RingCentral notification by the event filter it arrived on."""
    # The "event" string is the subscription's own event filter echoed back,
    # optionally with its query string (e.g. "?type=SMS") still attached.
    filter_path = (event.get('event') or '')
    if '/telephony/sessions' in filter_path:
        return process_telephony_session(event)
    elif '/message-store/instant' in filter_path:
        return process_instant_message(event)
    elif '/message-store' in filter_path:
        return process_message_store(event)

    logger.info("Ignoring RingCentral notification on %s", filter_path)
    return None


def process_message_store(event):
    """A batch of new/updated messages: bulk-fetch the new ones, if any."""
    body = event.get('body') or {}
    extension_id = body.get('extensionId')
    message_ids = extract_new_message_ids(body)
    if not message_ids:
        return None

    controller = RingCentralController()
    messages = controller.get_messages(extension_id, message_ids)

    logger.info("Fetched %d new message(s) for extension %s",
                len(messages), extension_id)
    return messages


def extract_new_message_ids(body):
    """Flatten newMessageIds across every change in one message-store notification."""
    ids = []
    for change in body.get('changes') or []:
        ids.extend(change.get('newMessageIds') or [])
    return ids


def process_instant_message(event):
    """A single new inbound SMS delivered in full: keep it in Slash and on the board."""
    message = extract_sms_fields(event)
    logger.info("Received instant SMS message %s", message)

    text = message['text']
    if not text:
        logger.info("Instant SMS %s carries no text, nothing to record", message['id'])
        return message

    phone = message['from']
    timestamp = datetime.fromisoformat(message['creation_time'])

    get_slash_controller().create_text_message(
        phone=phone, text=text, timestamp=timestamp, outbound=False)

    lines = text.replace("\n", "<br>")
    body = (f"{SELF_AUTHORED_UPDATE_MARKER}INBOUND SMS · {internal_timestamp(timestamp)}"
            f"<br>{lines}")
    get_monday_controller().create_update_to_cl(phone=phone, body=body)
    return message


def extract_sms_fields(event):
    """Pull the SMS fields we care about out of an instant message-store event."""
    body = event.get('body') or {}
    return {
        'id': body.get('id'),
        'extension_id': event.get('ownerId'),
        'from': (body.get('from') or {}).get('phoneNumber'),
        'to': [t.get('phoneNumber') for t in body.get('to') or []],
        'text': body.get('subject'),
        'direction': body.get('direction'),
        'creation_time': body.get('creationTime'),
    }


def process_telephony_session(event):
    """One state change on a call: parse it, no fetch needed."""
    session = extract_telephony_fields(event)
    logger.info("Received telephony session event %s", session)
    return session


def extract_telephony_fields(event):
    """Pull the call fields we care about out of a telephony session event."""
    body = event.get('body') or {}
    return {
        'session_id': body.get('telephonySessionId'),
        'sequence': body.get('sequence'),
        'event_time': body.get('eventTime'),
        'parties': [
            {
                'id': party.get('id'),
                'extension_id': party.get('extensionId'),
                'direction': party.get('direction'),
                'status': (party.get('status') or {}).get('code'),
                'from': (party.get('from') or {}).get('phoneNumber'),
                'to': (party.get('to') or {}).get('phoneNumber'),
            }
            for party in body.get('parties') or []
        ],
    }
