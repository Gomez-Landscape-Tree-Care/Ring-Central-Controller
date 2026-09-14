"""Delete one RingCentral webhook subscription by id.

Run list_subscriptions.py first to find the id. This is what retires a
subscription that has been repointed at a new address: creating the new one does
not replace the old, and both keep delivering until this runs.
"""
import sys

from ringcentral import SDK

from config import RC_CLIENT_ID, RC_CLIENT_SECRET, RC_JWT, RC_SERVER

if len(sys.argv) != 2:
    sys.exit("usage: python delete_subscription.py <subscription-id>")

subscription_id = sys.argv[1]

rcsdk = SDK(RC_CLIENT_ID, RC_CLIENT_SECRET, RC_SERVER)
platform = rcsdk.platform()
platform.login(jwt=RC_JWT)
platform.delete(f"/restapi/v1.0/subscription/{subscription_id}")
print(f"Deleted subscription {subscription_id}")
