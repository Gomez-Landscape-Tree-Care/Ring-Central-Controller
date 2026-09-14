"""List this app's RingCentral webhook subscriptions.

The delivery address is what to read: it says which Lambda each event filter is
pointed at, and a filter appearing twice is two live subscriptions delivering
the same notification.
"""
import json

from ringcentral import SDK

from config import RC_CLIENT_ID, RC_CLIENT_SECRET, RC_JWT, RC_SERVER

rcsdk = SDK(RC_CLIENT_ID, RC_CLIENT_SECRET, RC_SERVER)
platform = rcsdk.platform()
platform.login(jwt=RC_JWT)
r = platform.get("/restapi/v1.0/subscription")
print(json.dumps(json.loads(r.text()), indent=2))
