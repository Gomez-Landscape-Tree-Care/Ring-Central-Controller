"""Lambda entrypoint for the Ring Central Controller.

Consumes Ring Central telephony notifications off the FIFO queue, and receives
message-store webhooks and requests from the CL (ClientsLeads) and AB (Applicants
Board) Lambdas directly over a Function URL.
"""

import json

from utils.http_utils import get_body, response
from logger_config import logger
from services.ring_central.workflow import process_ring_central


def _process_queue_records(records):
    """One notification per record — the event source is configured BatchSize 1.

    Failures are left to raise so the message goes back on the queue and lands in
    the dead letter queue once it runs out of receives.
    """
    for record in records:
        process_ring_central(json.loads(record["body"]))


def lambda_handler(event, context):
    logger.info(event)

    records = event.get("Records") or []
    if records and records[0].get("eventSource") == "aws:sqs":
        return _process_queue_records(records)

    request = event.get("requestContext", {}).get("http", {})
    method = request.get("method")
    path = request.get("path", "/")

    # Header names arrive lowercased in the Function URL payload.
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # Ring Central subscription handshake: echo the validation token back.
    validation_token = headers.get("validation-token")
    if validation_token:
        logger.info("Ring Central subscription handshake for %s", path)
        return response(200, {}, {"Validation-Token": validation_token})

    # TODO: authenticate the caller before doing any work. The Function URL is
    # public (AuthType NONE) — verify the Ring Central verification token on
    # webhooks and a shared secret on CL/AB requests.

    body = get_body(event)
    logger.info("Request %s %s", method, path)

    # A RingCentral webhook notification always carries subscriptionId; a
    # CL/AB request never will.
    if "subscriptionId" in body:
        process_ring_central(body)
    # TODO: route CL/AB requests to services.slash.

    return response(200, {"ok": True})
