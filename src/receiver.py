"""Lambda entrypoint for the Ring Central webhook receiver.

Accepts Ring Central telephony session notifications over a Function URL and puts
them on the FIFO queue the processor consumes. No Ring Central API work happens
here — that belongs on the far side of the queue.
"""

import json
import os
import re

import boto3

from utils.http_utils import get_body, response
from logger_config import logger

sqs = boto3.client("sqs")

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
    # (AuthType NONE) — verify the Ring Central verification token.

    notification = get_body(event)
    if "subscriptionId" not in notification:
        logger.info("Ignoring request without a subscriptionId")
        return response(200, {"ok": True})

    group_id, deduplication_id = _fifo_keys(notification)
    sqs.send_message(
        QueueUrl=os.environ["EVENT_QUEUE_URL"],
        MessageBody=json.dumps(notification),
        MessageGroupId=group_id,
        MessageDeduplicationId=deduplication_id,
    )
    logger.info("Enqueued %s in group %s", deduplication_id, group_id)

    return response(200, {"ok": True})
