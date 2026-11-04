"""Persistence errors shared by local and AWS repository adapters."""


class ClientNotFoundError(LookupError):
    """Raised when a client identifier cannot be found."""


class ToolError(RuntimeError):
    """Raised when an authoritative business data source is unavailable."""
