"""Lambda entrypoint for the Ring Central Controller.

Receives Ring Central webhooks (inbound/outbound calls and SMS) and requests from
the CL (ClientsLeads) and AB (Applicants Board) Lambdas over a Function URL.
"""

import base64
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

# Resolved SSM values, cached for the life of the execution environment.
_param_cache = {}


def _get_param(env_var_name):
    """Resolve the SSM parameter whose path is held in ``env_var_name``."""
    if env_var_name in _param_cache:
        return _param_cache[env_var_name]

    path = os.environ[env_var_name]
    response = ssm.get_parameter(Name=path, WithDecryption=True)
    value = response["Parameter"]["Value"]
    _param_cache[env_var_name] = value
    return value


def get_config():
    """Return the Ring Central and Slash credentials from SSM."""
    return {
        "rc_client_secret": _get_param("RC_CLIENT_SECRET_PARAM"),
        "rc_jwt": _get_param("RC_JWT_PARAM"),
        "slash_service_password": _get_param("SLASH_SERVICE_PASSWORD_PARAM"),
    }


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

    # TODO: route to the Ring Central webhook handler vs. the CL/AB handler and
    # drive services.ring_central / services.slash using get_config().
    del body

    return _response(200, {"ok": True})
