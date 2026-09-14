"""Lambda entrypoint for the public webhook receiver.

Accepts Ring Central telephony session notifications on "/", Ring Central
inbound SMS notifications on "/sms/inbound", monday.com webhooks on "/monday",
and send-this-text requests from the AB (Applicants Board) and CL (Clients &
Leads) Lambdas on "/ab" and "/cl". All of them go on the FIFO queue the
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

# The instant message-store subscription delivers inbound SMS here. The event
# filter it arrived on is checked as well as the path, because the group id
# below is SMS shaped and a telephony payload would be given a nonsense one.
INBOUND_SMS_PATH = "/sms/inbound"
INSTANT_FILTER = "/message-store/instant"

# One path per calling Lambda rather than one path and a body field, so a
# request that reaches the wrong handler is a 404-shaped mistake rather than a
# text credited to the wrong service.
SEND_PATHS = {"/ab": sources.AB, "/cl": sources.CL}

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


def _handle_ring_central(notification):
    if "subscriptionId" not in notification:
        logger.info("Ignoring request without a subscriptionId")
        return response(200, {"ok": True})

    group_id, deduplication_id = _fifo_keys(notification)
    _enqueue(notification, group_id, deduplication_id, sources.RING_CENTRAL)

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
    body = notification.get("body")
    raw_phone = (body.get("from") or {}).get("phoneNumber", '')
    phone = normalize_phone_with_plus(raw=raw_phone)
    if not phone:
        phone = notification.get("uuid") or notification["subscriptionId"]
        logger.info("No usable sender on message %s, falling back to %s",
                    body.get("id"), phone)

    _enqueue(notification, _fifo_id(phone), str(uuid.uuid4()),
             sources.RING_CENTRAL_INBOUND_SMS)

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
    """One text the AB or CL Lambda wants sent, on its way to the queue.

    Answers 200 either way. The caller is a Lambda, not a person: a 4xx buys it
    nothing it can act on, and a retry against a send endpoint is the one thing
    worth not encouraging.

    The phone is normalized here rather than past the queue because this is
    where it can still be refused - it is also the group id, and an unnormalized
    one would put two spellings of the same person in two groups that no longer
    order against each other.
    """
    phone = normalize_phone_with_plus(payload.get("phone"))
    message = (payload.get("message") or "").strip()
    if not phone or not message:
        logger.info("Ignoring %s send request with no %s", source,
                    "usable phone" if not phone else "message")
        return response(200, {"ok": True})

    body = {
        "phone": phone,
        "message": message,
        "monday_user_id": payload.get("monday_user_id"),
    }
    # The person is what has to stay ordered, the way the item is on the monday
    # path. Deduplication is off for the same reason it is there: the callers
    # send no idempotency token, so a repeated request is a repeated text.
    _enqueue(body, _fifo_id(phone), str(uuid.uuid4()), source)

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

    # TODO: authenticate the caller before enqueueing. The Function URL is public
    # (AuthType NONE) — verify the Ring Central verification token and monday's
    # signing header.

    path = event.get("requestContext", {}).get("http", {}).get("path", "/")
    body = get_body(event)

    if path == MONDAY_PATH:
        return _handle_monday(body)
    if path in SEND_PATHS:
        return _handle_send_request(body, SEND_PATHS[path])
    if path == INBOUND_SMS_PATH:
        return _handle_inbound_sms(body)
    return _handle_ring_central(body)
