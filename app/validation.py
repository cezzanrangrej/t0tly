from __future__ import annotations
import ipaddress
import re
from urllib.parse import urlparse
from app.config import settings

ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{3,30}$")
MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = {"http", "https"}

# Private / loopback / link-local ranges (SSRF guard)
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class ValidationError(Exception):
    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


def _is_private_host(host: str) -> bool:
    """Return True if host is a private/loopback IP address."""
    h = host.strip("[]")  # strip IPv6 brackets
    try:
        addr = ipaddress.ip_address(h)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return False  # hostname — allow (no DNS at write time)


def _self_referential(url: str) -> bool:
    """Return True if url points back at this service."""
    parsed_base = urlparse(settings.base_url)
    parsed_url = urlparse(url)
    base_host = parsed_base.netloc.lower().split(":")[0]
    url_host = parsed_url.netloc.lower().split(":")[0]
    if not url_host:
        return False
    if url_host == base_host:
        return True
    # Treat localhost and 127.0.0.1 as equivalent for local dev/testing
    if base_host in ("localhost", "127.0.0.1") and url_host in ("localhost", "127.0.0.1"):
        return True
    return False


def validate_url(url: str) -> str:
    if not url:
        raise ValidationError("URL must not be empty")
    if len(url) > MAX_URL_LENGTH:
        raise ValidationError(f"URL exceeds {MAX_URL_LENGTH} characters")
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValidationError(
            f"URL scheme must be http or https, got '{parsed.scheme}'"
        )
    if not parsed.netloc:
        raise ValidationError("URL has no host")
    host = parsed.hostname or ""
    if _is_private_host(host):
        raise ValidationError(
            "URL resolves to a private/loopback address (SSRF guard)"
        )
    if _self_referential(url):
        raise ValidationError("URL must not point back at this service")
    return url


def validate_alias(alias: str) -> str:
    if not ALIAS_RE.match(alias):
        raise ValidationError("Alias must be 3-30 characters of [A-Za-z0-9_-]")
    if alias.lower() in [r.lower() for r in settings.reserved_codes]:
        raise ValidationError(f"Alias '{alias}' is reserved")
    return alias
