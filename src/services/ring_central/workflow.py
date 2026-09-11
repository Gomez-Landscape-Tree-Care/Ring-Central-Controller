"""Route inbound RingCentral webhook notifications by event type."""

from datetime import datetime

from config import SELF_AUTHORED_UPDATE_MARKER
from logger_config import logger
from services.monday.controller import get_controller as get_monday_controller
from services.ring_central.controller import RingCentralController
from services.slash.controller import get_controller as get_slash_controller
from utils.date import internal_timestamp

# An MMS carries its own body as a text/plain part, and can carry a vCard or a
# voice clip next to the photos. Only the images are worth putting on a board.
PHOTO_CONTENT_PREFIX = "image/"


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
    """A single new inbound SMS or MMS delivered in full: keep it in Slash and on the boards."""
    message = extract_sms_fields(event)
    logger.info("Received instant SMS message %s", message)

    text = message['text']
    attachments = message['attachments']
    if not text and not attachments:
        logger.info("Instant SMS %s carries nothing to record", message['id'])
        return message

    phone = message['from']
    timestamp = datetime.fromisoformat(message['creation_time'])

    if phone != '+16264888229':
        return

    if text:
        get_slash_controller().create_text_message(
            phone=phone, text=text,
            timestamp=timestamp, outbound=False)

    # Fetched once, before the updates are written, so the body can say what the
    # updates actually carry rather than what the message claimed to have. Both
    # boards get the same bytes - downloading per board would fetch each photo
    # twice for every message.
    photos = RingCentralController().download_attachments(attachments)
    body = inbound_body(text, timestamp)

    monday = get_monday_controller()
    update_ids = (monday.create_update_to_cl(phone=phone, body=body),
                  monday.create_update_to_ab(phone=phone, body=body))
    for photo in photos:
        for update_id in update_ids:
            monday.add_photo_to_update(
                update_id=update_id, filename=photo.filename,
                content=photo.content, content_type=photo.content_type)
    return message

def inbound_body(text, timestamp) -> str:
    """The update as monday renders it - HTML, so newlines have to be <br>."""
    header = "INBOUND SMS"
    lines = [f"{SELF_AUTHORED_UPDATE_MARKER}{header} · {internal_timestamp(timestamp)}"]
    if text:
        lines.append(text.replace("\n", "<br>"))
    return "<br>".join(lines)

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
        'attachments': image_attachments(body),
    }


def image_attachments(body) -> list[dict]:
    """The photo parts of an MMS record, trimmed to what a download needs.

    Filtered on contentType rather than on `type`: the message body arrives as an
    attachment too (type Text, contentType text/plain) next to the MmsAttachment
    parts, and `subject` already carries it. A vCard or a voice clip is an
    MmsAttachment as much as a photo is, so `type` alone cannot tell them apart -
    the media type can, and it is also what names the file and what decides
    whether monday shows a thumbnail rather than a file chip.

    Metadata only, no bytes: process_instant_message logs this whole dict.
    """
    photos = []
    for attachment in body.get('attachments') or []:
        content_type = attachment.get('contentType') or ''
        if not content_type.startswith(PHOTO_CONTENT_PREFIX):
            logger.info("Skipping %s attachment %s",
                        content_type or attachment.get('type'), attachment.get('id'))
            continue
        photos.append({
            'id': attachment.get('id'),
            'uri': attachment.get('uri'),
            'content_type': content_type,
            'filename': attachment.get('fileName'),
            'size': attachment.get('size'),
        })
    return photos


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
