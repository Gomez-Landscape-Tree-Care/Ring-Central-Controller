"""Create a RingCentral webhook subscription for instant (inbound) SMS messages.

Delivered to the receiver, which puts it on the FIFO queue. The outbound
message-store subscription still goes straight at the controller - its payload
carries only message ids, and resolving those to a phone needs RingCentral
credentials the receiver does not have.
"""
import json

from ringcentral import SDK

from config import RC_CLIENT_ID, RC_CLIENT_SECRET, RC_JWT, RC_SERVER

body = {
    "eventFilters": [
        "/restapi/v1.0/account/~/extension/~/message-store/instant?type=SMS"
    ],
    "expiresIn": 315360000,
    "deliveryMode": {
        "address": "https://gbtiklkmpxldp74yu7r4olnwqm0qrqzk.lambda-url.us-west-1.on.aws/sms/inbound",
        "transportType": "WebHook",
    }
}

rcsdk = SDK(RC_CLIENT_ID, RC_CLIENT_SECRET, RC_SERVER)
platform = rcsdk.platform()
platform.login(jwt=RC_JWT)
r = platform.post("/restapi/v1.0/subscription", body)
print(json.dumps(json.loads(r.text()), indent=2))
