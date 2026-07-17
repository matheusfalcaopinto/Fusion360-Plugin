"""Fail-closed endpoint validation for Fusion MCP transports and probes."""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit


Resolver = Callable[[str, int, int, int], Iterable[tuple]]
REMOTE_POLICIES = ("loopback_only", "allowlist")


@dataclass(frozen=True, slots=True)
class EndpointDecision:
    """Validated endpoint metadata safe to retain in diagnostics."""

    endpoint: str
    policy: str
    host: str
    port: int
    scheme: str
    resolved_ips: tuple[str, ...]
    loopback: bool
    requires_bearer_token: bool


class EndpointPolicyError(ValueError):
    """Raised before networking when an endpoint violates local policy."""

    code = "ENDPOINT_POLICY_BLOCKED"


class RejectHttpRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib handler that never converts a 3xx response into a new request."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def open_url_no_redirects(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> Any:
    """Open one validated URL while rejecting every redirect response."""

    # Never inherit HTTP(S)_PROXY for a local authority decision. An ambient
    # proxy could otherwise redirect a validated request to a different sink.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        RejectHttpRedirectHandler(),
    )
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise EndpointPolicyError(
                "HTTP redirect responses are not allowed"
            ) from exc
        raise
    status_value = getattr(response, "status", None)
    status = int(status_value if status_value is not None else response.getcode())
    if 300 <= status < 400:
        response.close()
        raise EndpointPolicyError("HTTP redirect responses are not allowed")
    return response


def validate_endpoint(
    endpoint: str,
    *,
    policy: str | None = None,
    allowlist: str | Iterable[str] | None = None,
    bearer_token: str | None = None,
    resolver: Resolver = socket.getaddrinfo,
) -> EndpointDecision:
    """Validate scheme, credentials, DNS results, allowlist and remote auth.

    Loopback HTTP is permitted because Autodesk's desktop server is local.
    Non-loopback transports require the explicit ``allowlist`` policy, HTTPS,
    an allowlisted literal address, and an environment-provided bearer token.
    Hostnames fail closed until the HTTP transport can pin the approved DNS
    result at the socket boundary.
    """

    configured_policy = (
        (policy or os.getenv("FUSION_AGENT_REMOTE_POLICY", "loopback_only"))
        .strip()
        .lower()
    )
    if configured_policy not in REMOTE_POLICIES:
        raise EndpointPolicyError(
            "FUSION_AGENT_REMOTE_POLICY must be loopback_only or allowlist"
        )

    parsed = urlsplit(endpoint)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise EndpointPolicyError("Fusion MCP endpoint must use http or https")
    if not parsed.hostname:
        raise EndpointPolicyError("Fusion MCP endpoint must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise EndpointPolicyError(
            "credentials must not be embedded in the endpoint URL"
        )
    if parsed.query:
        raise EndpointPolicyError(
            "endpoint URL query strings are not allowed; tokens must come from the environment"
        )
    if parsed.fragment:
        raise EndpointPolicyError("endpoint URL fragments are not allowed")

    host = parsed.hostname.rstrip(".").lower()
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise EndpointPolicyError(f"invalid endpoint port: {exc}") from exc
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        # DNS validation followed by a hostname-based client connection still
        # permits a second, different resolution at the socket boundary. Until
        # the transport can pin the approved address while preserving TLS SNI,
        # hostname endpoints fail closed.
        raise EndpointPolicyError(
            "Fusion MCP hostname endpoints are disabled; use a literal IP address"
        ) from exc
    resolved = _resolve_ips(host, port, resolver)
    loopback = bool(resolved) and all(
        ipaddress.ip_address(value).is_loopback for value in resolved
    )
    if loopback:
        return EndpointDecision(
            endpoint=endpoint,
            policy=configured_policy,
            host=host,
            port=port,
            scheme=parsed.scheme.lower(),
            resolved_ips=resolved,
            loopback=True,
            requires_bearer_token=False,
        )

    if configured_policy != "allowlist":
        raise EndpointPolicyError("non-loopback Fusion MCP endpoints are disabled")
    if parsed.scheme.lower() != "https":
        raise EndpointPolicyError("non-loopback Fusion MCP endpoints require HTTPS")

    entries = _allowlist_entries(
        allowlist
        if allowlist is not None
        else os.getenv("FUSION_AGENT_REMOTE_ALLOWLIST", "")
    )
    if not entries:
        raise EndpointPolicyError("remote endpoint allowlist is empty")
    if not _endpoint_is_allowlisted(host, resolved, entries):
        raise EndpointPolicyError(
            "remote endpoint host or resolved address is not allowlisted"
        )

    token = (
        bearer_token
        if bearer_token is not None
        else os.getenv("FUSION_MCP_BEARER_TOKEN")
    )
    if not token or not token.strip():
        raise EndpointPolicyError("remote endpoint requires FUSION_MCP_BEARER_TOKEN")

    return EndpointDecision(
        endpoint=endpoint,
        policy=configured_policy,
        host=host,
        port=port,
        scheme=parsed.scheme.lower(),
        resolved_ips=resolved,
        loopback=False,
        requires_bearer_token=True,
    )


def revalidate_resolution(
    decision: EndpointDecision,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> None:
    """Detect DNS changes before opening or reconnecting a transport."""

    current = _resolve_ips(decision.host, decision.port, resolver)
    if current != decision.resolved_ips:
        raise EndpointPolicyError(
            "endpoint DNS resolution changed after validation; refusing possible rebinding"
        )


def _resolve_ips(host: str, port: int, resolver: Resolver) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            records = resolver(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except OSError as exc:
            raise EndpointPolicyError(
                f"endpoint hostname could not be resolved: {exc}"
            ) from exc
        addresses = {
            str(ipaddress.ip_address(str(record[4][0]).split("%", 1)[0]))
            for record in records
            if len(record) > 4 and record[4]
        }
    else:
        addresses = {str(literal)}
    if not addresses:
        raise EndpointPolicyError("endpoint hostname resolved to no addresses")
    return tuple(sorted(addresses))


def _allowlist_entries(value: str | Iterable[str]) -> tuple[str, ...]:
    raw = value.split(",") if isinstance(value, str) else list(value)
    return tuple(item.strip().lower().rstrip(".") for item in raw if item.strip())


def _endpoint_is_allowlisted(
    host: str, resolved: tuple[str, ...], entries: tuple[str, ...]
) -> bool:
    host_entries: set[str] = set()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            host_entries.add(entry)
    if host in host_entries:
        return True
    if not networks:
        return False
    return all(
        any(ipaddress.ip_address(address) in network for network in networks)
        for address in resolved
    )
