"""Signed media URLs (nginx secure_link compatible).

Without signing, a node URL handed to a client is a permanent public download
link: anyone who copies it out of a browser's network tab can redistribute the
file forever.

The scheme is nginx's ``secure_link_md5`` with the expiry form::

    secure_link $arg_<digest>,$arg_<expires>;
    secure_link_md5 "$secure_link_expires$uri$arg_r$arg_u <secret>";

``r`` is the per-user bandwidth cap in bytes/second (0 = uncapped) and ``u``
is an anonymised user tag for the node-side speed collector.  Both live
*inside* the digest: a client that edits its own rate or identity off the URL
gets a 403, not a faster stream.

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


def user_tag(user_id: str) -> str:
    """Anonymised, stable tag for one Emby user.

    Goes into node access logs, so it must not be the raw account id; ten hex
    chars keep collisions irrelevant at this fleet's scale.
    """
    if not user_id:
        return ""
    return hashlib.md5(str(user_id).encode()).hexdigest()[:10]


def compute_digest(decoded_path: str, expires: int, secret: str,
                   rate_bps: int | None = None, utag: str = "") -> str:
    """base64url(md5("<expires><uri><r><u> <secret>")) without padding.

    ``rate_bps=None`` reproduces the legacy expression (no r/u in the string),
    kept so verify() can still check URLs minted before the rate rollout.
    """
    if not decoded_path.startswith("/"):
        decoded_path = "/" + decoded_path
    extra = "" if rate_bps is None else f"{int(rate_bps)}{utag}"
    raw = f"{expires}{decoded_path}{extra} {secret}".encode()
    digest = hashlib.md5(raw).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def sign_url(base_url: str, decoded_path: str, secret: str, ttl: int,
             arg_digest: str = DEFAULT_ARG_DIGEST,
             arg_expires: str = DEFAULT_ARG_EXPIRES,
             now: float | None = None,
             rate_bps: int = 0, utag: str = "") -> str:
    """Build a signed, expiring URL for one media file on a node.

    ``rate_bps`` is the per-user bandwidth cap the node must enforce
    (bytes/second, 0 = uncapped); ``utag`` identifies the user to the node's
    speed collector without exposing the account id.
    """
    ttl = max(MIN_TTL, min(int(ttl), MAX_TTL))
    expires = int(time.time() if now is None else now) + ttl
    if not decoded_path.startswith("/"):
        decoded_path = "/" + decoded_path
    digest = compute_digest(decoded_path, expires, secret,
                            rate_bps=int(rate_bps), utag=utag)
    encoded = quote(decoded_path, safe="/")
    return (f"{base_url.rstrip('/')}{encoded}"
            f"?r={int(rate_bps)}&u={quote(utag)}"
            f"&{arg_expires}={expires}&{arg_digest}={digest}")


def public_url(base_url: str, decoded_path: str) -> str:
    if not decoded_path.startswith("/"):
        decoded_path = "/" + decoded_path
    return f"{base_url.rstrip('/')}{quote(decoded_path, safe='/')}"


def verify(decoded_path: str, digest: str, expires: int, secret: str,
           now: float | None = None,
           rate_bps: int | None = None, utag: str = "") -> bool:
    """Mirror of the nginx check, used by tests and the panel's self-check."""
    current = time.time() if now is None else now
    if expires < current:
        return False
    expected = compute_digest(decoded_path, expires, secret,
                              rate_bps=rate_bps, utag=utag)
    return secrets.compare_digest(expected, digest)
