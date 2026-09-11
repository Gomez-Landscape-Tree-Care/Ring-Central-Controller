"""Lambda entrypoint for the public webhook receiver.

Accepts Ring Central telephony session notifications on "/" and monday.com
webhooks on "/monday", and puts both on the FIFO queue the processor consumes.
No Ring Central or monday API work happens here — that belongs on the far side
of the queue.
"""

import json
import os
import re
import uuid

import boto3

import sources
from utils.http_utils import get_body, response
from logger_config import logger

sqs = boto3.client("sqs")

MONDAY_PATH = "/monday"
MONDAY_CREATE_UPDATE = "create_update"

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
    return _handle_ring_central(body)
