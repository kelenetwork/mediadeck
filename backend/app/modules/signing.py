"""Signed media URLs (nginx secure_link compatible).

Without signing, a node URL handed to a client is a permanent public download
link: anyone who copies it out of a browser's network tab can redistribute the
file forever.

The scheme is nginx's ``secure_link_md5`` with the expiry form::

    secure_link $arg_<digest>,$arg_<expires>;
    secure_link_md5 "$secure_link_expires$uri <secret>";

Two details are not cosmetic:

* nginx compares against the **decoded** ``$uri``, so the digest is computed
  over the decoded path and only the resulting URL is percent-encoded.  The
  reverse order 403s every path containing a space or CJK character, which is
  most of a Chinese media library.

* The query argument names are configurable per node.  A node that is already
  in production may use ``?k=&e=`` rather than ``?md5=&expires=``; hardcoding
  either one silently 403s every request on the other.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import quote

MIN_TTL = 60
MAX_TTL = 86400 * 7
DEFAULT_ARG_DIGEST = "md5"
DEFAULT_ARG_EXPIRES = "expires"


def generate_secret(length: int = 30) -> str:
    return secrets.token_urlsafe(length)


def compute_digest(decoded_path: str, expires: int, secret: str) -> str:
    """base64url(md5("<expires><uri> <secret>")) without padding, as nginx does."""
    if not decoded_path.startswith("/"):
        decoded_path = "/" + decoded_path
    raw = f"{expires}{decoded_path} {secret}".encode()
    digest = hashlib.md5(raw).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def sign_url(base_url: str, decoded_path: str, secret: str, ttl: int,
             arg_digest: str = DEFAULT_ARG_DIGEST,
             arg_expires: str = DEFAULT_ARG_EXPIRES,
             now: float | None = None) -> str:
    """Build a signed, expiring URL for one media file on a node."""
    ttl = max(MIN_TTL, min(int(ttl), MAX_TTL))
    expires = int(time.time() if now is None else now) + ttl
    if not decoded_path.startswith("/"):
        decoded_path = "/" + decoded_path
    digest = compute_digest(decoded_path, expires, secret)
    encoded = quote(decoded_path, safe="/")
    return (f"{base_url.rstrip('/')}{encoded}"
            f"?{arg_expires}={expires}&{arg_digest}={digest}")


def public_url(base_url: str, decoded_path: str) -> str:
    if not decoded_path.startswith("/"):
        decoded_path = "/" + decoded_path
    return f"{base_url.rstrip('/')}{quote(decoded_path, safe='/')}"


def verify(decoded_path: str, digest: str, expires: int, secret: str,
           now: float | None = None) -> bool:
    """Mirror of the nginx check, used by tests and the panel's self-check."""
    current = time.time() if now is None else now
    if expires < current:
        return False
    return secrets.compare_digest(compute_digest(decoded_path, expires, secret), digest)
