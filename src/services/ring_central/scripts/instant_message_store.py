"""Create a RingCentral webhook subscription for instant SMS messages."""
import json

from ringcentral import SDK

from config import RC_CLIENT_ID, RC_CLIENT_SECRET, RC_JWT, RC_SERVER

body = {
    "eventFilters": [
        "/restapi/v1.0/account/~/extension/~/message-store/instant?type=SMS"
    ],
    "expiresIn": 315360000,
    "deliveryMode": {
        "address": "https://iqjujgfa4ncw7pq6tjcult7ulm0jpant.lambda-url.us-west-1.on.aws/",
        "transportType": "WebHook",
    }
}

rcsdk = SDK(RC_CLIENT_ID, RC_CLIENT_SECRET, RC_SERVER)
platform = rcsdk.platform()
platform.login(jwt=RC_JWT)
r = platform.post("/restapi/v1.0/subscription", body)
print(json.dumps(json.loads(r.text()), indent=2))
