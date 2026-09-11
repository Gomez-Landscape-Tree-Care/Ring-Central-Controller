"""The record of which RingCentral messages this system has already handled.

A DynamoDB table keyed on the RingCentral message id and written with a
conditional put: the first writer of an id wins and every writer after it is
told to stand down. It answers one question - "is this message mine to record?"
- for two callers that would otherwise each need their own answer.

The controller registers every SMS it sends, so the Outbound message-store
webhook RingCentral fires straight back at us finds the id taken and stops,
rather than mirroring our own text onto the boards as though an agent had typed
it in the RingCentral app. process_message_store claims every id it is handed,
so a redelivered notification stops in the same place. Kept out of config.py for
the reason sources.py documents - config resolves SSM at import, and this module
has to stay importable by anything.

Entries expire after a day: the window only has to outlive RingCentral's own
redelivery, and a ledger kept forever would cost more to hold than the
duplicates it prevents.
"""

import os
import time

import boto3

from logger_config import logger

TTL_SECONDS = 24 * 60 * 60

dynamodb = boto3.client("dynamodb")


def _key(message_id) -> str:
    return str(message_id)


def claim(message_id) -> bool:
    """Take one RingCentral message id, answering whether this caller got it.

    True means nothing has seen the id before and the caller should go on to
    record the message; False means somebody already has and there is nothing
    left to do.

    Only the conditional check failure is caught. A throttle, a missing table or
    an expired credential is left to raise, because a ledger that fails open
    silently records every message twice - and on the webhook path the claim is
    the first side effect, so a raise here costs nothing that has already
    happened. The send side, where the text is on the wire by the time this
    runs, is the one caller that cannot afford that and catches for itself.

    A record with no id cannot be deduplicated at all, and DynamoDB will not
    take an empty partition key, so it is refused rather than let through.
    """
    if not message_id:
        logger.warning("Cannot claim a message with no id")
        return False

    try:
        dynamodb.put_item(
            TableName=os.environ["PROCESSED_MESSAGES_TABLE"],
            Item={
                "message_id": {"S": _key(message_id)},
                "expires_at": {"N": str(int(time.time()) + TTL_SECONDS)},
            },
            ConditionExpression="attribute_not_exists(message_id)",
        )
        logger.info(f'Claimed message {message_id}')
    except dynamodb.exceptions.ConditionalCheckFailedException:
        logger.info("Message %s already handled, skipping", message_id)
        return False

    return True


def release(message_id) -> None:
    """Give an id back, for work that was claimed and then never happened.

    Without this a message whose recording failed stays claimed for a day, and
    nothing - not a redelivery, not a later fetch - can pick it up again. The
    delete is best effort: the caller is already on a failure path, and a
    release that raised would replace a lost message with a lost invocation.
    """
    if not message_id:
        return None

    try:
        dynamodb.delete_item(
            TableName=os.environ["PROCESSED_MESSAGES_TABLE"],
            Key={"message_id": {"S": _key(message_id)}},
        )
    except Exception:
        logger.exception("Could not release claim on message %s", message_id)
    return None
