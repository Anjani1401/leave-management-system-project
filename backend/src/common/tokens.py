"""Signed tokens for the approve/reject links in approver emails.

Format: base64url(json payload) + "." + base64url(HMAC-SHA256 signature)

The signing key lives in AWS Secrets Manager (never in code). A token can't be
changed without breaking the signature, and it expires after `exp`.
"""
import base64
import hashlib
import hmac
import json
import os
import time

from common import aws

_secret_cache = {}


class TokenError(Exception):
    pass


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text):
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _secret():
    if os.environ.get("SIGNING_SECRET"):  # local testing only
        return os.environ["SIGNING_SECRET"].encode()
    arn = os.environ["SIGNING_SECRET_ARN"]
    if arn not in _secret_cache:
        value = aws.secrets().get_secret_value(SecretId=arn)["SecretString"]
        _secret_cache[arn] = value.encode()
    return _secret_cache[arn]


def sign(payload, ttl_seconds):
    body = dict(payload)
    body["exp"] = int(time.time()) + int(ttl_seconds)
    encoded = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(_secret(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{sig}"


def verify(token):
    if not token or not isinstance(token, str) or token.count(".") != 1:
        raise TokenError("This link is invalid.")
    encoded, sig = token.split(".")
    expected = _b64(hmac.new(_secret(), encoded.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise TokenError("This link is invalid or has been tampered with.")
    try:
        payload = json.loads(_unb64(encoded))
    except (ValueError, json.JSONDecodeError):
        raise TokenError("This link is invalid.") from None
    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenError("This link has expired.")
    return payload
