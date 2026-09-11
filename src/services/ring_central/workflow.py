"""Route inbound RingCentral webhook notifications by event type."""

from datetime import datetime

import ledger
from config import (JEFF_BOT_USER_ID, MONDAY_USERS, SELF_AUTHORED_UPDATE_MARKER,
                    TEST_PHONE)
from logger_config import logger
from services.monday.controller import get_controller as get_monday_controller
from services.ring_central.controller import RingCentralController
from services.slash.controller import get_controller as get_slash_controller
from utils.date import internal_timestamp

# An MMS carries its own body as a text/plain part, and can carry a vCard or a
# voice clip next to the photos. Only the images are worth putting on a board.
PHOTO_CONTENT_PREFIX = "image/"

OUTBOUND = "Outbound"

# A message RingCentral never got out is not one to put in front of anybody as
# though it had been sent.
FAILED_STATUSES = ("SendingFailed", "DeliveryFailed")

SMS_ENTRY_TYPE = "sms"


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
    """A batch of new messages - outbound, by this subscription's filter.

    The claim comes before the fetch, not after: every SMS this system sends
    arrives back here, and answering those with one conditional put is the whole
    point of the ledger. Gating on the phone first would mean fetching each one
    off RingCentral only to throw it away, and the phone is not knowable until
    the fetch has happened anyway.
    """
    body = event.get('body') or {}
    extension_id = body.get('extensionId')
    message_ids = [message_id for message_id in extract_new_message_ids(body)
                   if ledger.claim(message_id)]
    if not message_ids:
        return None

    records = RingCentralController().get_messages(extension_id, message_ids)
    logger.info("Fetched %d of %d claimed message(s) for extension %s",
                len(records), len(message_ids), extension_id)

    # get_messages drops an id it could not fetch rather than raising, so the
    # ones that did not come back are given up - a claim nothing ever recorded
    # would otherwise hold the message out of reach until the entry expires.
    fetched = {str(record.get('id')) for record in records}
    for message_id in message_ids:
        if str(message_id) not in fetched:
            ledger.release(message_id)

    messages = []
    for record in records:
        message = sms_fields(record, extension_id)
        try:
            messages.append(record_sms(message))
        except Exception:
            # Released and swallowed rather than raised: RingCentral retires a
            # subscription that keeps answering 5xx, so an outage that took the
            # handler down with it would cost the webhook itself. Per record, so
            # one bad message does not take the rest of the batch with it.
            ledger.release(message['id'])
            logger.exception("Recording SMS %s failed, claim released",
                             message['id'])
    return messages


def extract_new_message_ids(body):
    """Flatten newMessageIds across every change in one message-store notification."""
    ids = []
    for change in body.get('changes') or []:
        ids.extend(change.get('newMessageIds') or [])
    return ids


def process_instant_message(event):
    """A single new SMS or MMS delivered in full: keep it in Slash and on the boards.

    No ledger claim here. This subscription is the inbound half of the pair and
    the outbound one is filtered on direction, so the two never carry the same
    message - which is also why process_ring_central can route on the event
    filter alone.
    """
    message = sms_fields(event.get('body') or {}, event.get('ownerId'))
    logger.info("Received instant SMS message %s", message)
    return record_sms(message)


def record_sms(message):
    """Keep one SMS, in whichever direction it went, in Slash and on both boards.

    An SMS an agent typed in the RingCentral app is worth what an inbound one
    is - the same Slash message, the same two board updates, the same photos -
    so both directions run this and differ only in what sms_fields already
    worked out for them.

    The rollout gate is here as well as inside both controllers, and it is the
    one that matters: this is the copy that runs before download_attachments, so
    a photo belonging to somebody outside the rollout is never pulled off
    RingCentral at all.
    """
    text = message['text']
    attachments = message['attachments']
    if not text and not attachments:
        logger.info("SMS %s carries nothing to record", message['id'])
        return message

    if message['status'] in FAILED_STATUSES:
        logger.info("SMS %s was not sent (%s), leaving it off the boards",
                    message['id'], message['status'])
        return message

    phone = message['phone']
    if phone != TEST_PHONE:
        return message

    outbound = message['outbound']
    timestamp = datetime.fromisoformat(message['creation_time'])

    if text:
        get_slash_controller().create_text_message(
            phone=phone, text=text, timestamp=timestamp, outbound=outbound,
            monday_user_id=JEFF_BOT_USER_ID if outbound else None)

    if outbound:
        sender = MONDAY_USERS[JEFF_BOT_USER_ID]['informal_name']
        get_slash_controller().create_timeline_entry(
            phone=phone,
            entry=f"{internal_timestamp(timestamp)} - {sender} sent a text message",
            entry_type=SMS_ENTRY_TYPE,
            timestamp=timestamp,
            outbound=True)

    # Fetched once, before the updates are written, so the body can say what the
    # updates actually carry rather than what the message claimed to have. Both
    # boards get the same bytes - downloading per board would fetch each photo
    # twice for every message.
    photos = RingCentralController().download_attachments(attachments)
    body = update_body(text, timestamp, outbound)

    monday = get_monday_controller()
    update_ids = (monday.create_update_to_cl(phone=phone, body=body),
                  monday.create_update_to_ab(phone=phone, body=body))
    for photo in photos:
        for update_id in update_ids:
            monday.add_photo_to_update(
                update_id=update_id, filename=photo.filename,
                content=photo.content, content_type=photo.content_type)
    return message


def update_body(text, timestamp, outbound) -> str:
    """The update as monday renders it - HTML, so newlines have to be <br>.

    The marker leads an outbound update as much as an inbound one, and not
    because the two look alike: process_create_update reads the webhook these
    writes fire, and an outbound update without the marker comes back
    indistinguishable from something an agent typed on the board meaning for us
    to text it out.
    """
    header = None if outbound else f"INBOUND SMS · {internal_timestamp(timestamp)}"
    lines = []
    if header:
        lines.append(header)
    if text:
        lines.append(text.replace("\n", "<br>"))
    lines.append(SELF_AUTHORED_UPDATE_MARKER)
    return "<br>".join(lines)


def sms_fields(record, extension_id):
    """Pull the SMS fields we care about out of one message-store record.

    The record rather than the notification, because the two subscriptions hand
    it over differently: the instant one delivers the whole record inline, and
    the other delivers ids that get_message answers with the same shape. The
    extension id comes in for the same reason - instant carries it as the
    notification's ownerId, and the other path only knows it from the change the
    ids arrived on. It is str()ed because RING_USERS is keyed by string and the
    notification may put a number there.

    `phone` is the other party, which is not the same field in both directions:
    an inbound SMS is from them and an outbound one is to them. Settling it here
    is what leaves record_sms with nothing to branch on. A message with several
    recipients has no single other party, so the first is taken and the rest
    logged - as close as a per-person board can get.
    """
    sender = (record.get('from') or {}).get('phoneNumber')
    recipients = [t.get('phoneNumber') for t in record.get('to') or []
                  if t.get('phoneNumber')]
    outbound = record.get('direction') == OUTBOUND
    if outbound and len(recipients) > 1:
        logger.info("Message %s has %d recipients, recording against %s",
                    record.get('id'), len(recipients), recipients[0])

    return {
        'id': record.get('id'),
        'extension_id': str(extension_id or ''),
        'from': sender,
        'to': recipients,
        'phone': (recipients[0] if recipients else None) if outbound else sender,
        'text': record.get('subject'),
        'direction': record.get('direction'),
        'outbound': outbound,
        'status': record.get('messageStatus'),
        'creation_time': record.get('creationTime'),
        'attachments': image_attachments(record),
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
