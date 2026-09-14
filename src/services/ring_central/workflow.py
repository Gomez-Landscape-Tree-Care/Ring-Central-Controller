"""Route inbound RingCentral webhook notifications, and send SMS back out.

Both halves live here because they are the same message twice: record_sms
writes down a text that already happened, and send_and_record puts one on the
wire and then writes it down the same way.
"""

from datetime import datetime, timezone

import ledger
from config import (JEFF_BOT_USER_ID, MAIN_COMPANY_LINE, MONDAY_USERS,
                    RING_USERS, SELF_AUTHORED_UPDATE_MARKER, TEST_PHONE)
from logger_config import logger
from services.monday.controller import get_controller as get_monday_controller
from services.ring_central.controller import RingCentralController
from services.ring_central.model import MAX_SMS_CHARS
from services.slash.controller import get_controller as get_slash_controller
from utils.date import internal_timestamp
from utils.normalize import normalize_phone_with_plus
from utils.timeline import render_timeline

# An MMS carries its own body as a text/plain part, and can carry a vCard or a
# voice clip next to the photos. Only the images are worth putting on a board.
PHOTO_CONTENT_PREFIX = "image/"

OUTBOUND = "Outbound"

# A message RingCentral never got out is not one to put in front of anybody as
# though it had been sent.
FAILED_STATUSES = ("SendingFailed", "DeliveryFailed")

SMS_ENTRY_TYPE = "sms"
CALL_ENTRY_TYPE = "call"

# The party statuses worth a timeline line. A call passes through Setup,
# Proceeding, Answered, Disconnected and sometimes Hold or VoiceMail; these two
# are the ones somebody reading a board wants to know about.
PROCEEDING = "Proceeding"
ANSWERED = "Answered"


def process_ring_central(event):
    """Route one RingCentral notification by the event filter it arrived on."""
    # The "event" string is the subscription's own event filter echoed back,
    # optionally with its query string (e.g. "?type=SMS") still attached.
    filter_path = (event.get('event') or '')
    if '/telephony/sessions' in filter_path:
        return process_telephony_session(event)

    logger.info("Ignoring RingCentral notification on %s", filter_path)
    return None


def process_outbound_message(payload):
    """One outbound SMS off the queue: claim the id, fetch it, record it.

    The claim comes before the fetch, as it did when these arrived a batch at a
    time: every SMS this service sends comes back here, and answering those with
    one conditional put is the whole point of the ledger. Gating on the phone
    first would mean fetching a message off RingCentral only to throw it away,
    and the phone is not knowable until the fetch has happened.

    Failures raise, where process_message_store below swallows them. That one
    answers RingCentral, which retires a subscription that keeps returning 5xx;
    this one answers the queue, where a raise is a redelivery and then the dead
    letter queue. The claim is released first either way - a redelivery that
    found the id still taken would drop the message and report success.
    """
    message_id = payload.get('message_id')
    extension_id = payload.get('extension_id')
    if not message_id or not extension_id:
        # Returned rather than raised: a payload this malformed will not fetch on
        # the third attempt either, and three receives buys only a dead letter
        # nobody can act on.
        logger.warning("Outbound SMS payload carries no %s, nothing to fetch",
                       "message id" if not message_id else "extension id")
        return None

    if not ledger.claim(message_id):
        return None

    records = RingCentralController().get_messages(extension_id, [message_id])
    if not records:
        # Raised rather than given up on. get_messages swallows a failed fetch so
        # one bad id cannot cost its neighbours, and with a single id there are
        # no neighbours left to protect - what it hides now is the difference
        # between a message that was purged and RingCentral being briefly
        # unreachable. Only the queue can tell those apart, by trying again.
        ledger.release(message_id)
        raise RuntimeError(
            f"Could not fetch message {message_id} on extension {extension_id}")

    message = sms_fields(records[0], extension_id)
    logger.info("Received outbound SMS message %s", message)
    try:
        return record_sms(message)
    except Exception:
        ledger.release(message_id)
        raise


def extract_new_message_ids(body):
    """Flatten newMessageIds across every change in one message-store notification."""
    ids = []
    for change in body.get('changes') or []:
        ids.extend(change.get('newMessageIds') or [])
    return ids


def process_inbound_message(event):
    """A single new SMS or MMS delivered in full: keep it in Slash and on the boards.

    No ledger claim here. This subscription is the inbound half of the pair and
    the outbound one is filtered on direction, so the two never carry the same
    message - which is also why process_ring_central can route on the event
    filter alone.
    """
    message = sms_fields(event.get('body') or {}, event.get('ownerId'))
    logger.info("Received inbound SMS message %s", message)
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

    # Up front, once: the updates below and the Timeline column rebuild write to
    # the same two items, and resolve_items is the board search both would
    # otherwise pay for separately.
    monday = get_monday_controller()
    items = monday.resolve_items(phone)

    if text:
        get_slash_controller().create_text_message(
            phone=phone, text=text, timestamp=timestamp, outbound=outbound,
            monday_user_id=JEFF_BOT_USER_ID if outbound else None)

    # Fetched once, before the updates are written, so the body can say what the
    # updates actually carry rather than what the message claimed to have. Both
    # boards get the same bytes - downloading per board would fetch each photo
    # twice for every message.
    photos = RingCentralController().download_attachments(attachments)
    body = update_body(text, timestamp, outbound)

    update_ids = [monday.create_update(board, item_id, body)
                  for board, item_id in items]
    for photo in photos:
        for update_id in update_ids:
            monday.add_photo_to_update(
                update_id=update_id, filename=photo.filename,
                content=photo.content, content_type=photo.content_type)

    if outbound:
        sender = MONDAY_USERS[JEFF_BOT_USER_ID]['informal_name']
        # Unstamped, as every entry Slash holds is: render_timeline stamps each
        # line from the row's own timestamp, so a stamp in the text itself would
        # come back doubled on the rebuild below.
        get_slash_controller().create_timeline_entry(
            phone=phone,
            entry=f"{sender} sent a text message",
            entry_type=SMS_ENTRY_TYPE,
            timestamp=timestamp,
            outbound=True)
        rebuild_timelines(phone, items)
    return message


def process_send_request(payload, source):
    """A CL or AB Lambda asking for one text to go out.

    The phone arrives normalized and the message non-empty - the receiver settles
    both before enqueueing, since a request it could not act on is not worth a
    queue message. Checked again anyway, because the queue is the trust boundary
    this side of it and a send is not a thing to attempt on a half-formed record.

    Stamped here rather than off the request: a send request carries no time of
    its own, and what the Slash row and the Timeline line are recording is the
    moment the text went out.
    """
    phone = payload.get('phone')
    text = (payload.get('message') or "").strip()
    if not phone or not text:
        logger.info("%s send request carries no %s", source,
                    "phone" if not phone else "message")
        return None

    return send_and_record(
        phone, text,
        timestamp=datetime.now(timezone.utc),
        author_id=str(payload.get('monday_user_id') or ''),
        op=f"Text {source} request to {phone}")


def send_and_record(phone, text, *, timestamp, author_id, op="", skip_board=None):
    """Send one SMS and put it everywhere an outbound text belongs.

    The outbound twin of record_sms, and the one send path the service has:
    an agent typing an update and a CL/AB request both end here.

    An SMS is the one thing this service does that cannot be taken back, and
    every caller arrives over the FIFO queue, which redelivers whatever raises -
    so everything that can fail is done before the send and nothing after it is
    allowed to raise. That ordering is the at-most-once guarantee, not a
    tidiness: a raise past send_sms would put a second copy of the text on a real
    person's phone.

    `skip_board` is for the caller whose board already shows the message - the
    agent's own item carries what they typed. Skipped by board rather than by
    item id, because two items on one board can carry the same phone and
    resolve_items answers with the first, which need not be the one they typed on.
    """
    # The rollout gate the others cannot stand in for: resolve_items and the
    # Slash controller each hold one, but neither is on the send path, and this
    # is the only place a number outside the rollout would be texted rather than
    # merely written about.
    if phone != TEST_PHONE:
        logger.info("%s is outside the rollout, leaving the text unsent", phone)
        return None

    # Cut here rather than leaving it to the model's own slice, so the Slash row
    # and the board updates carry what went on the wire rather than what was asked.
    if len(text) > MAX_SMS_CHARS:
        logger.warning("Text to %s is %d chars, sending the first %d",
                       phone, len(text), MAX_SMS_CHARS)
        text = text[:MAX_SMS_CHARS]

    # Up front, once, as record_sms resolves before its writes: the rebuild and
    # the updates below write to the same items, and this is the board search
    # they would otherwise pay for separately.
    monday = get_monday_controller()
    items = monday.resolve_items(phone)

    RingCentralController().send_sms(text=text, to_number=phone, op=op)

    slash = get_slash_controller()
    try:
        slash.create_text_message(
            phone=phone, text=text, timestamp=timestamp, outbound=True,
            monday_user_id=author_id)
    except Exception:
        logger.exception("Slash message failed for %s, already sent", phone)

    try:
        slash.create_timeline_entry(
            phone=phone,
            entry=timeline_entry(author_id),
            entry_type=SMS_ENTRY_TYPE,
            timestamp=timestamp,
            outbound=True)
    except Exception:
        logger.exception("Slash timeline entry failed for %s, already sent", phone)

    rebuild_timelines(phone, items)

    body = update_body(text, timestamp, True)
    for board, item_id in items:
        if skip_board is None or board.id != skip_board.id:
            monday.create_update(board, item_id, body)
    return None


def timeline_entry(author_id) -> str:
    """The Timeline line for an SMS sent off a board or a CL/AB request.

    An author MONDAY_USERS has no row for is named by nobody rather than by Jeff
    Bot: the fallback stands in for the name, not for the person, and crediting
    somebody else's text to the bot is worse than leaving it unattributed.
    """
    sender = MONDAY_USERS.get(author_id, {}).get('informal_name')
    return f"{sender} sent a text message" if sender else "Someone sent a text message"


def rebuild_timelines(phone, items) -> None:
    """Re-render the Timeline column on each of `items` from what Slash holds.

    Read back rather than appended to: Slash is the record this service and both
    board automations write to, so rebuilding is what puts the other two's
    entries on a column this one is touching, and what lets entries drop off the
    old end once the column runs past its cap. The entry for this very message
    was written a moment ago, so it comes back in the read.

    Fails closed. A read that raises and a person Slash has no entries for both
    write nothing: a rebuild replaces the whole column, so rendering an empty
    one over a real history is worse than leaving it a line stale - and since
    every caller records its entry first, no entries means something is wrong
    rather than that there is nothing to say.

    The read is swallowed where create_timeline_entry above it is not. By the
    time this runs the entry is safely in Slash and the boards are only behind,
    so a raise would buy a retry that appends a second copy of everything
    record_sms has already written.
    """
    if not items:
        return None

    try:
        entries = get_slash_controller().timeline_entries(phone)
    except Exception:
        logger.exception("Timeline read failed for %s, columns left alone", phone)
        return None

    if not entries:
        logger.warning("Slash holds no timeline entries for %s, columns left alone", phone)
        return None

    column = render_timeline([(entry.entry, entry.timestamp) for entry in entries])
    monday = get_monday_controller()
    for board, item_id in items:
        monday.write_timeline(board, item_id, column)
    return None


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
    """One state change on a call: put it on the boards if it is one worth a line.

    No fetch needed - a telephony notification carries the whole state change,
    where the outbound message-store one carries only ids. The writes are the
    ones record_sms makes on its outbound branch, and for the same reason: Slash
    is the record, and the Timeline column is rendered back out of it.

    Nothing past the Slash entry is allowed to raise. This arrives over the FIFO
    queue, so a raise is a redelivery, and the timeline route appends rather than
    upserts - rebuild_timelines and the writes under it already swallow their
    own failures, which is what keeps a failed column write from putting a second
    copy of the line into Slash.
    """
    session = extract_telephony_fields(event)
    logger.info("Received telephony session event %s", session)

    party = company_line_party(session)
    if not party:
        logger.info("Session %s never touched the main line, ignoring",
                    session['session_id'])
        return None

    outbound = party['outbound']
    status = party['status']
    if status not in (PROCEEDING, ANSWERED) or (outbound and status == ANSWERED):
        logger.info("Nothing to record for a %s %s party on session %s",
                    "outbound" if outbound else "inbound", status,
                    session['session_id'])
        return None

    # The switch for whose calls are tracked at all: an extension with no row
    # here is dropped rather than written onto a board as a raw id.
    ring_user = RING_USERS.get(party['extension_id'])
    if not ring_user:
        logger.info("Extension %s is not tracked, ignoring session %s",
                    party['extension_id'], session['session_id'])
        return None

    phone = party['phone']
    if not phone:
        logger.info("No usable other party on session %s", session['session_id'])
        return None

    # Ahead of the board search rather than left to the gates inside
    # resolve_items and the Slash controller: those keep the writes from
    # happening, where this keeps the reads from being paid for.
    if phone != TEST_PHONE:
        logger.info("%s is outside the rollout, leaving session %s off the boards",
                    phone, session['session_id'])
        return None

    monday = get_monday_controller()
    items = monday.resolve_items(phone)
    entry = call_entry(party, ring_user, monday, items)

    # Unstamped, as every entry Slash holds is: render_timeline stamps each line
    # from the row's own timestamp.
    get_slash_controller().create_timeline_entry(
        phone=phone,
        entry=entry,
        entry_type=CALL_ENTRY_TYPE,
        timestamp=event_time(session),
        outbound=outbound)
    rebuild_timelines(phone, items)
    return session


def company_line_party(session):
    """The party on this session with the main company line on one end, or None.

    Direction is settled off which end the main line is on rather than off the
    party's own `direction` field, and a session carrying it on neither end is
    given back as None rather than guessed at - an extension-to-extension call
    and an outbound call placed with an agent's direct DID both land there, and
    neither is one this service has a person to write about.

    The other party falls out of the same answer, since it is whichever end the
    main line is not: the caller on an inbound call, the callee on an outbound
    one.
    """
    for party in session['parties']:
        from_phone = normalize_phone_with_plus(party['from'])
        to_phone = normalize_phone_with_plus(party['to'])
        if to_phone == MAIN_COMPANY_LINE:
            outbound = False
        elif from_phone == MAIN_COMPANY_LINE:
            outbound = True
        else:
            continue
        return {
            **party,
            'outbound': outbound,
            'phone': from_phone if not outbound else to_phone,
            'caller_name': party['from_name'],
        }
    return None


def call_entry(party, ring_user, monday, items) -> str:
    """The Timeline line for one call, in whichever direction it went.

    Both directions name the other party off the board first: the item name is
    the one somebody chose for this person, where RingCentral's caller id is
    whatever the carrier handed over and is often missing or a company string.
    Inbound falls back to that caller id, since a number with no item is exactly
    the call the webhook's own name is worth having. Both then fall back to the
    number - a line that names a phone still says who the call was with, where
    "None is calling Vig" says nothing.
    """
    name = ring_user['name']
    if not party['outbound']:
        if party['status'] == ANSWERED:
            return f"{name} answered"
        caller = (board_name(monday, items) or party['caller_name']
                  or party['phone'])
        return f"{caller} is calling {name}"

    return f"{name} is calling {board_name(monday, items) or party['phone']}"


def board_name(monday, items) -> str | None:
    """The name on the first item `phone` resolved to, or None if there is none.

    Read off one board rather than every one: the items are the same person on
    each, so a second read would answer the same name for another API call.
    """
    if not items:
        return None
    board, item_id = items[0]
    return monday.item_name(board, item_id)


def event_time(session) -> datetime:
    """When RingCentral says the state changed, falling back to now.

    Aware, always, for trigger_time's reason: this is what render_timeline prints
    and what Slash orders the column by, and a naive stamp is read as the
    Lambda's own clock - UTC deployed, Pacific on a laptop.
    """
    raw = session['event_time']
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        logger.warning("Session %s has no usable eventTime (%r)",
                       session['session_id'], raw)
        return datetime.now(timezone.utc)


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
                'extension_id': str(party.get('extensionId') or ''),
                'direction': party.get('direction'),
                'status': (party.get('status') or {}).get('code'),
                'from': (party.get('from') or {}).get('phoneNumber'),
                'to': (party.get('to') or {}).get('phoneNumber'),
                'from_name': (party.get('from') or {}).get('name'),
                'to_name': (party.get('to') or {}).get('name'),
            }
            for party in body.get('parties') or []
        ],
    }
