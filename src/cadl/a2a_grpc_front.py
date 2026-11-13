"""The front listener of the gRPC binding of `cadl a2a-gateway` (Appendix
G, G.13), standard library only.

`grpcio` has no hook where a connection could be counted or refused before
its calls, so the gateway accepts the TCP connections of `--grpc-listen`
itself, in one asyncio thread, and splices each accepted connection,
byte for byte, to the gRPC server, which listens on a Unix socket in a
directory of mode 0700 that only the user of the gateway can reach (where
there are no Unix sockets, on 127.0.0.1 with a port chosen by the system).
TLS and mutual TLS are not terminated here: the bytes are passed on as
they come, so the gRPC server does the handshake, sees the client
certificate and keeps its identities as before.

The listener bounds:

- the open connections of one remote address (an IPv4 address, or the /64
  of an IPv6 address), of one IPv4 /24 or IPv6 /48, and in all; a
  connection beyond them is reset at once and recorded at the capped rate
  (`overloaded`, reason `connections`);
- the time to the first call: a connection on which no call has reached
  the gateway within the deadline (`--read-timeout`, counted from the
  accept) is reset and recorded (`read_timeout`, reason `grpc handshake`).
  This covers a client that sends nothing, one that never starts or never
  finishes TLS, one that sends its handshake byte by byte, and a half-open
  connection; after its first call the gRPC server closes a connection
  that is idle for 2 minutes and one whose keepalive ping is not answered.

The real client address is carried to the gRPC server: each connection to
the backend is made from its own address (a Unix socket bound to a path of
its own, or a TCP port of its own), which gRPC reports as the peer of its
calls, and `client` maps that peer back to the accepted connection. The
caps of the binding on calls not yet authenticated are therefore keyed by
the real client address, as before; a call whose peer is not a connection
of the listener is refused.
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import os
import shutil
import socket
import tempfile
import threading
from typing import Any

CHUNK = 1 << 16


def peer_key(host: str) -> str:
    """The key of the cap per address: an IPv4 address, or the /64 of an
    IPv6 address; an IPv4-mapped IPv6 address counts as its IPv4 address
    (as `_Server.peer_key` of the HTTP listener)."""
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return host
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def prefix_key(host: str) -> str:
    """The key of the cap per prefix: the /24 of an IPv4 address (also
    IPv4-mapped), the /48 of an IPv6 address."""
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return host
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    bits = 24 if ip.version == 4 else 48
    return str(ipaddress.ip_network(f"{ip}/{bits}", strict=False))


class Connection:
    """An accepted connection: the address of the client, and whether a
    call on it has reached the gateway."""

    __slots__ = ("host", "called", "client", "backend")

    def __init__(self, host: str) -> None:
        self.host = host
        self.called = False
        self.client: Any = None
        self.backend: Any = None


class Front:
    """The listener of `--grpc-listen` in front of the gRPC server.

    `gw` needs only `refused(event, why)`. Create it (it binds `listen`,
    so that an error comes before anything starts), give `backend` to the
    gRPC server, then `start()`; `stop()` closes every connection."""

    def __init__(self, gw, listen: tuple[str, int], deadline: float = 10.0,
                 max_connections: int = 64, max_per_address: int = 4,
                 max_per_prefix: int = 16, unix: bool | None = None) -> None:
        self.gw = gw
        self.deadline = deadline
        self.max_connections = max_connections
        self.max_per_address = max_per_address
        self.max_per_prefix = max_per_prefix
        self.unix = hasattr(socket, "AF_UNIX") if unix is None else unix
        self.lock = threading.Lock()
        self.total = 0
        self.address_count: dict[str, int] = {}
        self.prefix_count: dict[str, int] = {}
        self.peers: dict[str, Connection] = {}
        self.ids = itertools.count(1)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.stopping: asyncio.Event | None = None
        self.dir: str | None = None
        host, port = listen
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6 and ipaddress.ip_address(host).is_unspecified:
                # [::] takes IPv4 clients too, as the listener of gRPC did.
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind((host, port))
            sock.listen(128)
            sock.setblocking(False)
        except (OSError, ValueError):
            sock.close()
            raise
        self.sock = sock
        self.port = sock.getsockname()[1]
        if self.unix:
            self.dir = tempfile.mkdtemp(prefix="cadl-grpc-")    # mode 0700
            if len(self.dir) > 80 and os.path.isdir("/tmp"):
                # sun_path holds about 100 bytes.
                os.rmdir(self.dir)
                self.dir = tempfile.mkdtemp(prefix="cadl-grpc-", dir="/tmp")
            self.path = os.path.join(self.dir, "grpc")
            self.backend = "unix:" + self.path
        else:
            self.backend_port = 0
            self.backend = "127.0.0.1:0"

    def set_backend_port(self, port: int) -> None:
        """Without Unix sockets: the port that the gRPC server took."""
        self.backend_port = port

    # --- the peers of the gRPC server -------------------------------------------

    def client(self, peer: str) -> Connection | None:
        """The accepted connection whose calls gRPC reports with `peer`,
        marked as having carried a call; None for any other peer."""
        with self.lock:
            conn = self.peers.get(peer)
        if conn is not None:
            conn.called = True
        return conn

    # --- the thread -------------------------------------------------------------

    def start(self) -> None:
        ready = threading.Event()

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            try:
                self.loop.run_until_complete(self._main(ready))
            finally:
                self.loop.close()
        self.thread = threading.Thread(target=run, name="cadl-grpc-front", daemon=True)
        self.thread.start()
        ready.wait(10)

    def stop(self) -> None:
        """Stop accepting and reset every connection."""
        loop, stopping = self.loop, self.stopping
        if loop is not None and stopping is not None and self.thread is not None:
            try:
                loop.call_soon_threadsafe(stopping.set)
            except RuntimeError:
                pass                    # the loop has ended
            self.thread.join(10)
        else:
            self.sock.close()

    def cleanup(self) -> None:
        """Remove the directory of the Unix socket (after the gRPC server
        has stopped)."""
        if self.dir is not None:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None

    async def _main(self, ready: threading.Event) -> None:
        self.stopping = asyncio.Event()
        server = await asyncio.start_server(self._handle, sock=self.sock, backlog=128,
                                            limit=CHUNK)
        ready.set()
        try:
            await self.stopping.wait()
        finally:
            server.close()
            with self.lock:
                conns = list(self.peers.values())
            for conn in conns:
                for writer in (conn.client, conn.backend):
                    if writer is not None:
                        writer.transport.abort()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- one connection ---------------------------------------------------------

    def _take(self, key: str, prefix: str) -> bool:
        with self.lock:
            if self.address_count.get(key, 0) >= self.max_per_address \
                    or self.prefix_count.get(prefix, 0) >= self.max_per_prefix \
                    or self.total >= self.max_connections:
                return False
            self.total += 1
            self.address_count[key] = self.address_count.get(key, 0) + 1
            self.prefix_count[prefix] = self.prefix_count.get(prefix, 0) + 1
            return True

    def _release(self, key: str, prefix: str) -> None:
        with self.lock:
            self.total -= 1
            for table, k in ((self.address_count, key), (self.prefix_count, prefix)):
                n = table.get(k, 1) - 1
                if n > 0:
                    table[k] = n
                else:
                    table.pop(k, None)

    async def _connect(self):
        """A connection to the gRPC server from an address of its own:
        (reader, writer, the peer string that gRPC reports for it)."""
        loop = asyncio.get_running_loop()
        if self.unix:
            path = os.path.join(self.dir, f"c{next(self.ids)}")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.setblocking(False)
                s.bind(path)
                await loop.sock_connect(s, self.path)
            except BaseException:
                s.close()
                raise
            finally:
                try:
                    os.unlink(path)     # the bound name stays with the socket
                except OSError:
                    pass
            name = "unix:" + path
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setblocking(False)
                await loop.sock_connect(s, ("127.0.0.1", self.backend_port))
            except BaseException:
                s.close()
                raise
            name = f"ipv4:127.0.0.1:{s.getsockname()[1]}"
        reader, writer = await asyncio.open_connection(sock=s, limit=CHUNK)
        return reader, writer, name

    @staticmethod
    async def _pump(reader, writer) -> None:
        try:
            while True:
                data = await reader.read(CHUNK)
                if not data:
                    return
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            return

    async def _handle(self, reader, writer) -> None:
        peername = writer.get_extra_info("peername")
        host = str(peername[0]) if peername else ""
        key, prefix = peer_key(host), prefix_key(host)
        if not self._take(key, prefix):
            writer.transport.abort()
            self.gw.refused("overloaded", "connections")
            return
        conn = Connection(host)
        conn.client = writer
        name = None
        tasks: set = set()
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except OSError:
                    pass
            try:
                breader, bwriter, name = await self._connect()
            except OSError:
                return
            conn.backend = bwriter
            with self.lock:
                self.peers[name] = conn
            tasks = {asyncio.ensure_future(self._pump(reader, bwriter)),
                     asyncio.ensure_future(self._pump(breader, writer))}
            done, _ = await asyncio.wait(tasks, timeout=self.deadline,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                if not conn.called:
                    self.gw.refused("read_timeout", "grpc handshake")
                    writer.transport.abort()
                    bwriter.transport.abort()
                    return
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            if name is not None:
                with self.lock:
                    self.peers.pop(name, None)
            for w in (conn.client, conn.backend):
                if w is not None and not w.transport.is_closing():
                    w.close()       # after what has been written
            self._release(key, prefix)
