"""Lambda entrypoint for the Ring Central Controller.

Receives Ring Central webhooks (inbound/outbound calls and SMS) and requests from
the CL (ClientsLeads) and AB (Applicants Board) Lambdas over a Function URL.
"""

import base64
import json
import os

import boto3

from logger_config import logger
from services.ring_central.workflow import process_ring_central

ssm = boto3.client("ssm")

# Resolved SSM values, cached for the life of the execution environment.
_param_cache = {}

def _response(status_code, body=None, headers=None):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "body": json.dumps(body if body is not None else {}),
    }


def _get_body(event):
    """Return the request body as a dict, or an empty dict when there is none."""
    body = event.get("body")
    if not body:
        return {}
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)


def lambda_handler(event, context):
    request = event.get("requestContext", {}).get("http", {})
    method = request.get("method")
    path = request.get("path", "/")

    # Header names arrive lowercased in the Function URL payload.
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # Ring Central subscription handshake: echo the validation token back.
    validation_token = headers.get("validation-token")
    if validation_token:
        logger.info("Ring Central subscription handshake for %s", path)
        return _response(200, {}, {"Validation-Token": validation_token})

    # TODO: authenticate the caller before doing any work. The Function URL is
    # public (AuthType NONE) — verify the Ring Central verification token on
    # webhooks and a shared secret on CL/AB requests.

    body = _get_body(event)
    logger.info("Request %s %s", method, path)

    # A RingCentral webhook notification always carries subscriptionId; a
    # CL/AB request never will.
    if "subscriptionId" in body:
        process_ring_central(body)
    # TODO: route CL/AB requests to services.slash.

    return _response(200, {"ok": True})
