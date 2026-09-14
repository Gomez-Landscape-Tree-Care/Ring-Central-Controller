"""Lambda entrypoint for the Ring Central Controller.

Consumes Ring Central telephony, inbound SMS and outbound SMS notifications,
monday webhooks and CL (ClientsLeads) / AB (Applicants Board) send requests off
the FIFO queue. They all reach it through the receiver, which is the only
function that can write to that queue.

The Function URL is still served while the outbound message-store subscription
is being repointed at the receiver - the old subscription keeps delivering here
until it is deleted - and has nothing pointed at it once that is done.
"""

import json

import sources
from utils.http_utils import get_body, response
from logger_config import logger
from services.monday.workflow import process_monday
from services.ring_central.workflow import (process_inbound_message,
                                            process_outbound_message,
                                            process_ring_central,
                                            process_send_request)


def _process_queue_records(records):
    """One notification per record — the event source is configured BatchSize 1.

    Failures are left to raise so the message goes back on the queue and lands in
    the dead letter queue once it runs out of receives.
    """
    for record in records:
        attributes = record.get("messageAttributes") or {}
        source = (attributes.get("source") or {}).get("stringValue")
        body = json.loads(record["body"])
        # Messages enqueued before the source attribute existed are Ring Central.
        if source == sources.MONDAY:
            process_monday(body)
        elif source in (sources.AB, sources.CL):
            process_send_request(body, source)
        elif source == sources.RING_CENTRAL_INBOUND_SMS:
            process_inbound_message(body)
        elif source == sources.RING_CENTRAL_OUTBOUND_SMS:
            process_outbound_message(body)
        else:
            process_ring_central(body)


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
    # public (AuthType NONE) — verify the Ring Central verification token.

    body = get_body(event)
    logger.info("Request %s %s", method, path)

    # A RingCentral webhook notification always carries subscriptionId. Nothing
    # else is served here: CL/AB send requests go to the receiver, which is the
    # function that can put them on the queue.
    if "subscriptionId" in body:
        process_ring_central(body)

    return response(200, {"ok": True})
