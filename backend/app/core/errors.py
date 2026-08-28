"""Domain errors surfaced to the API layer."""
from __future__ import annotations


class ConfigError(ValueError):
    """Operator-supplied configuration is invalid (maps to HTTP 422)."""


class NotConfigured(RuntimeError):
    """A required integration has not been connected yet (maps to HTTP 409)."""


class EmbyNotConfigured(NotConfigured):
    """Emby credentials are missing or the integration is disabled."""


class UpstreamError(RuntimeError):
    """A configured upstream answered, but not successfully (maps to HTTP 502)."""
