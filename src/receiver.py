"""Lambda entrypoint for the public webhook receiver.

Accepts Ring Central telephony session notifications on "/", Ring Central
inbound SMS notifications on "/sms/inbound" and outbound ones on "/sms/outbound",
monday.com webhooks on "/monday", and send-this-text requests from the AB
(Applicants Board) and CL (Clients & Leads) Lambdas on "/ab" and "/cl". All of
them go on the FIFO queue the
processor consumes. No Ring Central or monday API work happens here — that
belongs on the far side of the queue.
"""

import json
import os
import re
import uuid

import boto3

import sources
from utils.http_utils import get_body, response
from utils.normalize import normalize_phone_with_plus
from logger_config import logger

sqs = boto3.client("sqs")

MONDAY_PATH = "/monday"
MONDAY_CREATE_UPDATE = "create_update"

INBOUND_SMS_PATH = "/sms/inbound"
OUTBOUND_SMS_PATH = "/sms/outbound"
CALLS_PATH = '/calls'

SEND_PATHS = {
                "/ab": sources.AB, 
                "/cl": sources.CL
            }

# FIFO ids take alphanumerics and punctuation, up to 128 characters.
FIFO_ID_MAX_CHARS = 128
_UNSAFE_FIFO_CHARS = re.compile(r"[^A-Za-z0-9!-~]")


def _fifo_id(value):
    return _UNSAFE_FIFO_CHARS.sub("-", value)[:FIFO_ID_MAX_CHARS]


def _fifo_keys(notification):
    """Return the (group, deduplication) ids for one notification.

    Ring Central emits a notification per party for the same session and status,
    so the status is what makes a delivery unique while the session is what has
    to stay ordered.
    """
    body = notification.get("body") or {}
    session_id = body.get("telephonySessionId")
    parties = body.get("parties") or []
    status = ((parties[0] if parties else {}).get("status") or {}).get("code")

    if not session_id or not status:
        fallback = notification.get("uuid") or notification["subscriptionId"]
        logger.info("No telephony session keys, falling back to %s", fallback)
        return _fifo_id(fallback), _fifo_id(fallback)

    return _fifo_id(session_id), _fifo_id(f"{session_id}:{status}")


def _enqueue(body, group_id, deduplication_id, source):
    sqs.send_message(
        QueueUrl=os.environ["EVENT_QUEUE_URL"],
        MessageBody=json.dumps(body),
        MessageGroupId=group_id,
        MessageDeduplicationId=deduplication_id,
        MessageAttributes={"source": {"DataType": "String", "StringValue": source}},
    )
    logger.info("Enqueued %s %s in group %s", source, deduplication_id, group_id)


def _handle_calls(notification):
    if "subscriptionId" not in notification:
        logger.info("Ignoring request without a subscriptionId")
        return response(200, {"ok": True})

    group_id, deduplication_id = _fifo_keys(notification)
    _enqueue(notification, group_id, deduplication_id, sources.RING_CENTRAL_CALLS)

    return response(200, {"ok": True})


def _handle_inbound_sms(notification):
    """One inbound SMS, grouped by the number it came from.

    The person is what has to stay ordered, and the normalized phone is the same
    group id _handle_send_request uses, so a text from somebody and a text going
    back out to them order against each other rather than racing.

    Deduplication is off, as it is on the monday and send paths: nothing here
    is a repeat worth collapsing, and a text dropped because it looked like one
    already seen is worse than one recorded twice.
    """
    body = notification.get("body") or {}
    raw_phone = (body.get("from") or {}).get("phoneNumber", '')
    phone = normalize_phone_with_plus(raw=raw_phone)
    if not phone:
        phone = notification.get("uuid") or notification["subscriptionId"]
        logger.info("No usable sender on message %s, falling back to %s",
                    body.get("id"), phone)

    _enqueue(notification, _fifo_id(phone), str(uuid.uuid4()),
             sources.RING_CENTRAL_INBOUND_SMS)

    return response(200, {"ok": True})


def _handle_outbound_sms(notification):
    """The ids of one or more outbound SMS, grouped by the extension that sent them.

    One queue message per id rather than one per notification, because the
    deduplication id is per message - and the Ring Central message id is the
    only natural one this service gets on any path. It collapses a redelivery
    for five minutes where the ledger past the queue collapses it for a day.

    The group is the extension because the payload offers nothing better: this
    subscription carries ids and no phone, and resolving one to a person needs
    Ring Central credentials this function does not have. Outbound texts from an
    extension therefore order against each other rather than against the person
    they went to, which is what the phone-keyed groups elsewhere buy.

    The body is trimmed to what the far side reads, the way _handle_send_request
    trims a send request: the extension, and one id to fetch with it.
    """
    body = notification.get("body") or {}
    message_ids = [message_id
                   for change in body.get("changes") or []
                   for message_id in change.get("newMessageIds") or []
                   if message_id]
    if not message_ids:
        logger.info("No new messages on this message-store notification")
        return response(200, {"ok": True})

    # ownerId is the same extension under another name - process_instant_message
    # reads it off the envelope for exactly this. Worth preferring to the uuid
    # below, which can group a notification but leaves nothing to fetch against.
    extension_id = body.get("extensionId") or notification.get("ownerId")
    if extension_id:
        group_id = _fifo_id(f"ext-{extension_id}")
    else:
        # Prefixed for the same reason: a bare numeric group id is also what the
        # monday path builds out of an item id, and two sources sharing a group
        # would queue behind each other for nothing.
        group_id = _fifo_id("ext-" + str(notification.get("uuid")
                                         or notification.get("subscriptionId")
                                         or uuid.uuid4()))
        logger.info("No extension on this notification, falling back to %s", group_id)

    for message_id in message_ids:
        _enqueue({"extension_id": extension_id, "message_id": message_id},
                 group_id, _fifo_id(str(message_id)),
                 sources.RING_CENTRAL_OUTBOUND_SMS)

    return response(200, {"ok": True})


def _handle_monday(payload):
    """Answer the subscription challenge, or enqueue one create_update event.

    monday posts {"challenge": "<token>"} when the webhook is registered and marks
    the subscription inactive unless it is echoed back. Real events carry no
    idempotency token, so deduplication is disabled with a random id, and the item
    is what has to stay ordered.
    """
    challenge = payload.get("challenge")
    if challenge:
        logger.info("monday subscription handshake")
        return response(200, {"challenge": challenge})

    event = payload.get("event") or {}
    trigger = event.get("type")
    if trigger != MONDAY_CREATE_UPDATE:
        logger.info("Ignoring monday webhook of type %s", trigger)
        return response(200, {"ok": True})

    item_id = str(event.get("pulseId") or event.get("itemId") or "monday-unknown")
    _enqueue(payload, _fifo_id(item_id), str(uuid.uuid4()), sources.MONDAY)

    return response(200, {"ok": True})


def _handle_send_request(payload, source):
    """Texts the AB or CL Lambda wants sent, on its way to the queue.

    Answers 200 either way. The caller is a Lambda, not a person: a 4xx buys it
    nothing it can act on, and a retry against a send endpoint is the one thing
    worth not encouraging.
    """
    recipients = payload['recipients']
    message = payload["message"]
    message_type = payload['message_type']
    if not recipients or not message:
        logger.info("Ignoring %s send request with no %s", source,
                    "recipients" if not recipients else "message")
        return response(200, {"ok": True})

    body = {
        "recipients": recipients,
        "message": message,
        "monday_user_id": payload.get("monday_user_id"),
        "message_type": message_type,
        "client_phone": payload.get("client_phone")
    }
    # The person is what has to stay ordered, the way the item is on the monday
    # path. Deduplication is off for the same reason it is there: the callers
    # send no idempotency token, so a repeated request is a repeated text.
    _enqueue(body, 'send_request', str(uuid.uuid4()), source)

    return response(200, {"ok": True})


def lambda_handler(event, context):
    logger.info(event)

    # Header names arrive lowercased in the Function URL payload.
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # Ring Central subscription handshake: echo the validation token back.
    validation_token = headers.get("validation-token")
    if validation_token:
        logger.info("Ring Central subscription handshake")
        return response(200, {}, {"Validation-Token": validation_token})

    path = event.get("requestContext", {}).get("http", {}).get("path", "/")
    body = get_body(event)

    if path == MONDAY_PATH:
        return _handle_monday(body)
    if path in SEND_PATHS:
        return _handle_send_request(body, SEND_PATHS[path])
    if path == INBOUND_SMS_PATH:
        return _handle_inbound_sms(body)
    if path == OUTBOUND_SMS_PATH:
        return _handle_outbound_sms(body)
    if path == CALLS_PATH:
        return _handle_calls(body)

    return response(200, {'ok': True})
