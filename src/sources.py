"""Producer identifiers carried on the queue's `source` message attribute.

Kept out of config.py on purpose: config resolves SSM parameters at import time,
and the receiver function has none of those env vars.
"""

RING_CENTRAL = "ring-central"
RING_CENTRAL_INBOUND_SMS = "ring-central-inbound-sms"
MONDAY = "monday"
AB = "ab"
CL = "cl"
