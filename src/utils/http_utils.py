"""HTTP helpers shared by the Function URL entrypoints."""

import base64
import json


def response(status_code, body=None, headers=None):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "body": json.dumps(body if body is not None else {}),
    }


def get_body(event):
    """Return the request body as a dict, or an empty dict when there is none."""
    body = event.get("body")
    if not body:
        return {}
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)
