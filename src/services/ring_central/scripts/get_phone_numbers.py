"""List the phone numbers on this RingCentral extension."""
import json

from ringcentral import SDK

from config import RC_CLIENT_ID, RC_CLIENT_SECRET, RC_SERVER, RC_JWT


rcsdk = SDK(RC_CLIENT_ID, RC_CLIENT_SECRET, RC_SERVER)
platform = rcsdk.platform()
platform.login(jwt=RC_JWT)
r = platform.get("/restapi/v1.0/account/~/extension/~/phone-number")
# pretty print the text
print(json.dumps(json.loads(r.text()), indent=2))