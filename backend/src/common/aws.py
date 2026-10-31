"""Lazily created AWS clients.

Every handler gets its AWS clients from here instead of creating them at import
time. This keeps cold starts cheap and lets the unit tests swap in fakes with
`set_override(...)` without needing AWS credentials.
"""
import os

try:  # botocore is always present in the Lambda Python runtime
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - only used when running tests without boto3

    class ClientError(Exception):
        def __init__(self, error_response, operation_name="Unknown"):
            self.response = error_response
            self.operation_name = operation_name
            super().__init__(f"{error_response.get('Error', {}).get('Code')}: {operation_name}")


_cache = {}
_overrides = {}


def set_override(name, obj):
    """Used by tests: replace a client/resource with a fake."""
    _overrides[name] = obj


def reset():
    _cache.clear()
    _overrides.clear()


def _boto3():
    import boto3

    return boto3


def _get(name, factory):
    if name in _overrides:
        return _overrides[name]
    if name not in _cache:
        _cache[name] = factory()
    return _cache[name]


def dynamodb():
    return _get("dynamodb", lambda: _boto3().resource("dynamodb"))


def dynamodb_client():
    return _get("dynamodb_client", lambda: _boto3().client("dynamodb"))


def sfn():
    return _get("sfn", lambda: _boto3().client("stepfunctions"))


def ses():
    return _get("ses", lambda: _boto3().client("ses"))


def sns():
    return _get("sns", lambda: _boto3().client("sns"))


def secrets():
    return _get("secrets", lambda: _boto3().client("secretsmanager"))


def s3():
    def factory():
        from botocore.config import Config

        return _boto3().client(
            "s3",
            region_name=os.environ.get("AWS_REGION"),
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )

    return _get("s3", factory)


def error_code(err):
    """Return the AWS error code from a ClientError (or '' for other errors)."""
    response = getattr(err, "response", None) or {}
    return response.get("Error", {}).get("Code", "")
