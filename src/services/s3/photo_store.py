"""Copies of inbound MMS photos in the contractors app photo bucket.

Kept out of config.py for the reason sources.py documents - config resolves SSM
at import, and this module has to stay importable by anything.
"""

import os

import boto3

from logger_config import logger

KEY_PREFIX = "text-messages"

s3 = boto3.client("s3")


def upload_sms_photos(message_id, photos) -> list[str]:
    """Put each downloaded photo under text-messages/{message_id}/, answering the keys written.

    Best effort per photo, like download_attachments: the monday updates are the
    record, and a failed copy must not cost the message or its neighbours.
    """
    keys = []
    for photo in photos:
        key = f"{KEY_PREFIX}/{message_id}/{photo.filename}"
        try:
            s3.put_object(
                Bucket=os.environ["PHOTOS_BUCKET"],
                Key=key,
                Body=photo.content,
                ContentType=photo.content_type,
            )
            keys.append(key)
        except Exception:
            logger.exception("Could not upload photo %s", key)

    logger.info(f"Uploaded {len(keys)} of {len(photos)} photo(s) for message {message_id}")
    return keys
