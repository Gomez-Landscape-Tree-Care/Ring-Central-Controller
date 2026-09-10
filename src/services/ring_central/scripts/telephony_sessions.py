"""Create a RingCentral webhook subscription for telephony session events."""
import json

from ringcentral import SDK

from config import RC_CLIENT_ID, RC_CLIENT_SECRET, RC_JWT, RC_SERVER

# The receiver's Function URL, not the processor's — read it from the stack's
# ReceiverFunctionUrl output after deploying.
body = {
    "eventFilters": [
        "/restapi/v1.0/account/~/telephony/sessions"
    ],
    "expiresIn": 315360000,
    "deliveryMode": {
        "address": "https://gbtiklkmpxldp74yu7r4olnwqm0qrqzk.lambda-url.us-west-1.on.aws/",
        "transportType": "WebHook",
    }
}

rcsdk = SDK(RC_CLIENT_ID, RC_CLIENT_SECRET, RC_SERVER)
platform = rcsdk.platform()
platform.login(jwt=RC_JWT)
r = platform.post("/restapi/v1.0/subscription", body)
print(json.dumps(json.loads(r.text()), indent=2))
