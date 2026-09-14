"""Create a RingCentral webhook subscription for outbound SMS messages.

Delivered to the receiver, which puts each new message id on the FIFO queue
grouped by the extension that sent it. The phone is not in this payload - only
ids are - so resolving one to a text happens past the queue, where the
RingCentral credentials are.

Creating this does not retire the subscription pointed at the controller's own
URL: run list_subscriptions.py and then delete_subscription.py once this one is
delivering, or both keep firing.
"""
import json

from ringcentral import SDK

from config import RC_CLIENT_ID, RC_CLIENT_SECRET, RC_JWT, RC_SERVER

body = {
    "eventFilters": [
        "/restapi/v1.0/account/~/extension/~/message-store?type=SMS&direction=Outbound"
    ],
    "expiresIn": 315360000,
    "deliveryMode": {
        "address": "https://gbtiklkmpxldp74yu7r4olnwqm0qrqzk.lambda-url.us-west-1.on.aws/sms/outbound",
        "transportType": "WebHook",
    }
}

rcsdk = SDK(RC_CLIENT_ID, RC_CLIENT_SECRET, RC_SERVER)
platform = rcsdk.platform()
platform.login(jwt=RC_JWT)
r = platform.post("/restapi/v1.0/subscription", body)
print(json.dumps(json.loads(r.text()), indent=2))
