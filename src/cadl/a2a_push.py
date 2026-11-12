"""Push notifications at `cadl a2a-gateway` (Appendix G, G.13): which
callback URLs of clients the gateway calls, and how it calls them.

A2A 1.0.0 (https://a2a-protocol.org/v1.0.0/specification/) lets a client
register a webhook (3.1.7, 4.3) to which the agent POSTs `StreamResponse`
objects (4.3.3) with the credentials of the configuration, and asks the
caller of a webhook to validate the URL against server-side request
forgery: reject private ranges, localhost and link-local addresses, and
use allowlists (13.2). The gateway calls the client's URL itself, from
inside the network it protects, so it applies these checks, and more:

- the URL has to match an entry of the allowlist (`--push-allow`): a URL
  (scheme, host, port, and the path, or a path prefix when the entry ends
  with `/`), a host with an optional port, or an IP address or network;
- `https`, unless the host is a loopback address or `localhost`;
- every address that the host resolves to is a global unicast address,
  or lies in an address or network of the allowlist; an address of a cloud
  metadata service only when the allowlist names that address itself;
- the request goes to an address that was checked (no second lookup), is
  never redirected, has a deadline for the whole exchange, and reads at
  most `MAX_ANSWER` bytes of the answer.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

MAX_URL = 2048
MAX_ANSWER = 64 << 10           # bytes of a webhook's answer that are read
TIMEOUT = 10.0                  # seconds for one notification, from connect to answer

# Addresses of metadata services of clouds, which a network of the
# allowlist does not unlock; only the address itself does.
METADATA = {ipaddress.ip_address(a) for a in (
    "169.254.169.254", "169.254.170.2", "169.254.169.253", "100.100.100.200",
    "192.0.0.192", "fd00:ec2::254", "fd00:ec2::23")}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")
_TOKEN_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")      # RFC 9110 token
_VALUE_RE = re.compile(r"[\x21-\x7e]{1,4096}")


class PushError(ValueError):
    """A configuration or a URL that the gateway does not accept."""


def embedded(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """The IPv4 address inside an IPv4-mapped, NAT64, 6to4 or Teredo IPv6
    address, or the address itself."""
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64:
            return ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
        if ip.sixtofour is not None:
            return ip.sixtofour
        if ip.teredo is not None:
            return ip.teredo[1]
    return ip


def blocked(ip: ipaddress._BaseAddress) -> str | None:
    """Why an address is not a destination of its own: private, loopback,
    link-local, metadata and every other address that is not global
    unicast; None for a global unicast address."""
    for a in (embedded(ip), ip):         # the embedded IPv4 address first
        if a in METADATA:
            return "metadata"
        if a.is_loopback:
            return "loopback"
        if a.is_link_local:
            return "link-local"
        if a.is_private:
            return "private"
        if a.is_multicast or a.is_reserved or a.is_unspecified or not a.is_global:
            return "not global unicast"
    return None


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class _Url:
    scheme: str
    host: str
    port: int
    path: str           # path and query, as sent


def parse_url(url: Any) -> _Url:
    """A callback URL: http or https, a host, no user information, no
    fragment, printable ASCII of at most `MAX_URL` characters."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL \
            or not re.fullmatch(r"[\x21-\x7e]+", url):
        raise PushError("the URL is printable ASCII of at most 2048 characters")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise PushError("the URL is http or https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise PushError("the URL has no user information")
    if parts.fragment or url.endswith("#"):
        raise PushError("the URL has no fragment")
    host = (parts.hostname or "").lower()
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise PushError("the port of the URL is invalid") from None
    if not host:
        raise PushError("the URL has a host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOST_RE.fullmatch(host):
            raise PushError("the host of the URL is an ASCII host name or an IP "
                            "address") from None
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return _Url(parts.scheme, host, port, path)


@dataclass
class PushPolicy:
    """The allowlist of `--push-allow`, and the lookup of host names."""
    entries: list
    resolve: Callable[..., list] = socket.getaddrinfo
    urls: list = field(default_factory=list)          # _Url entries
    hosts: list = field(default_factory=list)         # (host, port or None)
    networks: list = field(default_factory=list)      # ip_network

    def __post_init__(self) -> None:
        if not self.entries:
            raise PushError("--push-allow names no URL, host or address")
        for entry in self.entries:
            if "://" in entry:
                try:
                    u = parse_url(entry)
                except PushError as e:
                    raise PushError(f"--push-allow {entry!r}: {e}") from None
                if "?" in u.path:
                    raise PushError(f"--push-allow {entry!r}: a URL entry has no query")
                self.urls.append(u)
                self._literal(u.host)
                continue
            try:
                net = ipaddress.ip_network(entry.strip("[]"), strict=True)
            except ValueError:
                net = None
            if net is not None:
                self.networks.append(net)
                if net.num_addresses == 1:
                    self.hosts.append((str(net.network_address), None))
                continue
            m = re.fullmatch(r"(\[[0-9A-Fa-f:.]+\]|[^:/\[\]]+)(?::([0-9]{1,5}))?", entry)
            if m is None:
                raise PushError(f"--push-allow {entry!r} is a URL, HOST[:PORT], or an IP "
                                f"address or network")
            host = m.group(1).strip("[]").lower()
            port = int(m.group(2)) if m.group(2) else None
            if port is not None and not 0 < port < 65536:
                raise PushError(f"--push-allow {entry!r}: the port is out of range")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if not _HOST_RE.fullmatch(host):
                    raise PushError(f"--push-allow {entry!r}: not an ASCII host name") \
                        from None
            self.hosts.append((host, port))
            self._literal(host)

    def _literal(self, host: str) -> None:
        """An IP address named as a host is allowed as an address too."""
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return
        self.networks.append(ipaddress.ip_network(ip))

    def check(self, url: Any) -> _Url:
        """The static checks of a callback URL: its form, https unless the
        host is loopback, and an entry of the allowlist that it matches."""
        u = parse_url(url)
        if u.scheme != "https" and not is_loopback_host(u.host):
            raise PushError("the URL is https (http only for a loopback host)")
        for e in self.urls:
            if (e.scheme, e.host, e.port) == (u.scheme, u.host, u.port):
                path = u.path.split("?", 1)[0]
                if path == e.path or (e.path.endswith("/") and path.startswith(e.path)):
                    return u
        for host, port in self.hosts:
            if host == u.host and (port is None or port == u.port):
                return u
        raise PushError("the URL matches no entry of --push-allow")

    def addresses(self, u: _Url) -> list[str]:
        """The addresses of the host of `u`, each checked; raises PushError
        when one of them is refused or the host has none."""
        try:
            infos = self.resolve(u.host, u.port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as e:
            raise PushError(f"the host does not resolve ({type(e).__name__})") from None
        out: list[str] = []
        for info in infos:
            text = str(info[4][0]).split("%", 1)[0]
            try:
                ip = ipaddress.ip_address(text)
            except ValueError:
                raise PushError("the host resolves to something that is not an IP "
                                "address") from None
            why = blocked(ip)
            if why is not None:
                listed = [n for n in self.networks if ip in n or embedded(ip) in n]
                if not listed or (why == "metadata"
                                  and not any(n.num_addresses == 1 for n in listed)):
                    raise PushError(f"the host resolves to an address that is {why} "
                                    f"({ip})")
            if text not in out:
                out.append(text)
        if not out:
            raise PushError("the host has no address")
        return out


def check_auth(token: Any, auth: Any) -> None:
    """The token and the authentication of a configuration (4.3.2): they
    become header values, so they are printable ASCII without line ends."""
    if token is not None and (not isinstance(token, str)
                              or (token and not _VALUE_RE.fullmatch(token))):
        raise PushError("token is a string of printable ASCII")
    if auth is None:
        return
    if not isinstance(auth, dict) or set(auth) - {"scheme", "credentials"} \
            or not isinstance(auth.get("scheme"), str) \
            or not _TOKEN_RE.fullmatch(auth["scheme"]):
        raise PushError("authentication is {\"scheme\": token, \"credentials\": string}")
    cred = auth.get("credentials")
    if cred is not None and (not isinstance(cred, str)
                             or (cred and not _VALUE_RE.fullmatch(cred))):
        raise PushError("authentication.credentials is a string of printable ASCII")


class _Pinned(http.client.HTTPConnection):
    """A connection to a checked address under the host name of the URL:
    the Host header, and for https the server name and the certificate,
    are those of the host name."""

    def __init__(self, u: _Url, ip: str, timeout: float, tls: ssl.SSLContext | None):
        super().__init__(u.host, u.port, timeout=timeout)
        self._ip, self._tls = ip, tls

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        if self._tls is not None:
            try:
                sock = self._tls.wrap_socket(sock, server_hostname=self.host)
            except BaseException:
                sock.close()
                raise
        self.sock = sock


def deliver(policy: PushPolicy, url: str, body: bytes, token: str | None,
            auth: dict | None, timeout: float = TIMEOUT,
            tls: ssl.SSLContext | None = None) -> tuple[int | None, str]:
    """POST one notification to a callback URL: (the HTTP status of a 2xx
    answer, "") or (None or the status, why it failed). The URL and the
    addresses of its host are checked again here, since a name can resolve
    differently later."""
    try:
        u = policy.check(url)
        ips = policy.addresses(u)
    except PushError as e:
        return None, f"refused: {e}"
    context = None
    if u.scheme == "https":
        context = tls or ssl.create_default_context()
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if token:
        headers["X-A2A-Notification-Token"] = token
    if auth is not None and auth.get("credentials"):
        headers["Authorization"] = f"{auth['scheme']} {auth['credentials']}"
    end = time.monotonic() + timeout
    last = "no address"
    for ip in ips:
        left = end - time.monotonic()
        if left <= 0:
            return None, "timeout"
        conn = _Pinned(u, ip, left, context)
        # The deadline holds for the whole exchange, also for a peer that
        # answers a byte at a time.
        timer = threading.Timer(left, lambda c=conn: _abort(c))
        timer.daemon = True
        timer.start()
        try:
            try:
                conn.connect()
            except (OSError, ssl.SSLError) as e:
                last = f"connect: {type(e).__name__}"
                continue
            try:
                conn.request("POST", u.path, body=body, headers=headers)
                resp = conn.getresponse()
                resp.read(MAX_ANSWER)
            except (OSError, ssl.SSLError, http.client.HTTPException) as e:
                if time.monotonic() >= end:
                    return None, "timeout"
                return None, f"{type(e).__name__}"
            if 200 <= resp.status < 300:
                return resp.status, ""
            if 300 <= resp.status < 400:
                return resp.status, f"redirect {resp.status} is not followed"
            return resp.status, f"HTTP status {resp.status}"
        finally:
            timer.cancel()
            conn.close()
    return None, last


def _abort(conn: http.client.HTTPConnection) -> None:
    sock = conn.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
