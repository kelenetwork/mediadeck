"""Signed media URLs (nginx secure_link compatible).

Without signing, a node URL handed to a client is a permanent public download
link: anyone who copies it out of a browser's network tab can redistribute the
file forever.  For a panel meant to be sold to operators running paid servers,
that is the difference between a product and a liability.

The scheme implemented is nginx's ``secure_link_md5`` with the widely used
expiry form::

    secure_link $arg_md5,$arg_expires;
    secure_link_md5 "$secure_link_expires$uri <secret>";

nginx compares against the *decoded* ``$uri``, so the digest is computed over
the decoded path and only the resulting URL is percent-encoded.  Getting that
order wrong yields 403s on any path containing spaces or CJK, which is most of
a Chinese media library.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import quote

MIN_TTL = 60
MAX_TTL = 86400 * 7


def generate_secret(length: int = 40) -> str:
    return secrets.token_urlsafe(length)


def compute_digest(decoded_path: str, expires: int, secret: str) -> str:
    """base64url(md5("<expires><uri> <secret>")) without padding, as nginx does."""
    if not decoded_path.startswith("/"):
        decoded_path = "/" + decoded_path
    raw = f"{expires}{decoded_path} {secret}".encode()
    digest = hashlib.md5(raw).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def sign_url(base_url: str, relative_path: str, secret: str, ttl: int,
             now: float | None = None) -> str:
    """Build a signed, expiring URL for one media file on a node."""
    ttl = max(MIN_TTL, min(int(ttl), MAX_TTL))
    expires = int(time.time() if now is None else now) + ttl
    decoded_path = "/" + relative_path.lstrip("/")
    digest = compute_digest(decoded_path, expires, secret)
    encoded = quote(decoded_path, safe="/")
    return f"{base_url.rstrip('/')}{encoded}?md5={digest}&expires={expires}"


def verify(decoded_path: str, digest: str, expires: int, secret: str,
           now: float | None = None) -> bool:
    """Mirror of the nginx check, used by tests and the panel's self-check."""
    current = time.time() if now is None else now
    if expires < current:
        return False
    return secrets.compare_digest(compute_digest(decoded_path, expires, secret), digest)
