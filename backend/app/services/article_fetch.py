"""Outbound article transport: public destinations only, with DNS pinning."""

import ipaddress
import socket
import ssl

import anyio
import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend


class UnsafeDestination(httpx.RequestError):
    pass


def public_address(value: str) -> str:
    address = ipaddress.ip_address(value)
    # Reject IPv6 transition mechanisms as well as non-public unicast addresses.
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or str(address) == "168.63.129.16"  # Azure platform virtual address.
        or (address.version == 6 and (
            address.is_site_local
            or address.ipv4_mapped is not None
            or address.sixtofour is not None
            or address.teredo is not None
            or address in ipaddress.ip_network("64:ff9b::/96")
            or address in ipaddress.ip_network("64:ff9b:1::/48")
        ))
    ):
        raise UnsafeDestination("Unsafe article destination.")
    return str(address)


def validate_url(value: str | httpx.URL) -> httpx.URL:
    try:
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
            raise ValueError
        host = url.host.rstrip(".").lower()
        if "%" in host or host == "localhost" or host.endswith(
            (".localhost", ".local", ".internal")
        ):
            raise UnsafeDestination("Unsafe article destination.")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass  # Hostnames are resolved and checked at connection time.
        else:
            public_address(host)
        return url
    except (httpx.InvalidURL, ValueError) as exc:
        raise UnsafeDestination("Invalid URL") from exc


class PublicNetworkBackend(AnyIOBackend):
    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        # One deadline includes DNS and all address connection attempts.
        try:
            with anyio.fail_after(timeout):
                try:
                    literal = ipaddress.ip_address(host)
                except ValueError:
                    records = await anyio.getaddrinfo(
                        host, port, type=socket.SOCK_STREAM,
                    )
                    addresses = list(dict.fromkeys(record[4][0] for record in records))
                else:
                    addresses = [str(literal)]
                if not addresses:
                    raise httpcore.ConnectError("Article DNS resolution failed.")
                # Fail closed for mixed public/private DNS answers, before dialing.
                addresses = [public_address(address) for address in addresses]
                for index, address in enumerate(addresses):
                    try:
                        # Numeric addresses cannot be rebound by a second DNS lookup.
                        # httpcore retains the original origin for Host, TLS SNI,
                        # certificate verification, and connection-pool isolation.
                        return await super().connect_tcp(
                            address, port, timeout=timeout,
                            local_address=local_address, socket_options=socket_options,
                        )
                    except httpcore.ConnectError:
                        if index == len(addresses) - 1:
                            raise
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("Article connection timed out.") from exc
        except OSError as exc:
            raise httpcore.ConnectError("Article DNS resolution failed.") from exc


class PublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self):
        super().__init__(trust_env=False)
        # HTTPX has no public network_backend argument. Keep the compatibility
        # seam here; reuse its response streaming and httpcore exception mapping.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=PublicNetworkBackend(),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
        )

    async def handle_async_request(self, request):
        validate_url(request.url)
        # Do not let a caller-supplied extension change certificate identity.
        request.extensions["sni_hostname"] = request.url.host
        return await super().handle_async_request(request)
