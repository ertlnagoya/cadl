"""The front listener of `cadl a2a-gateway --grpc-listen` (G.13): caps on
connections per address, per prefix and in all, the deadline to the first
call, the real client address carried to the gRPC server, and TLS that
passes through it.

In process against a small gRPC server (`Front` alone), and through the
gateway subprocess."""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import cadl.a2a_grpc_front as F
from cadl.a2a_grpc import hook_rejections
from cadl.a2a_grpc_front import Front, peer_key, prefix_key

from .a2a_certs import PKI
from .test_a2a_bindings import GrpcClient, events, grpc_message
from .test_a2a_gateway import Run, wait_for


@pytest.fixture
def grpc():
    return pytest.importorskip("grpc")


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return PKI(tmp_path_factory.mktemp("pki"), clients={"planner": "PLANNER"}, foreign={})


class Recorder:
    """The `refused` of a gateway."""

    def __init__(self) -> None:
        self.got: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def refused(self, event: str, why: str) -> None:
        with self.lock:
            self.got.append((event, why))

    def count(self, event: str, why: str) -> int:
        with self.lock:
            return self.got.count((event, why))


class Backend:
    """A Front in front of a gRPC server whose one method answers with the
    address that the Front reports for the peer of the call."""

    def __init__(self, grpc, unix: bool = True, deadline: float = 1.5, options=(),
                 listen: str = "127.0.0.1", max_rpcs: int | None = None, **caps) -> None:
        self.grpc = grpc
        self.gw = Recorder()
        self.front = Front(self.gw, (listen, 0), deadline=deadline, unix=unix, **caps)
        assert hook_rejections(self.front)

        def who(request, context):
            conn = self.front.client(context.peer())
            return (conn.host if conn is not None else "unknown").encode()

        def hang(request, context):
            self.front.client(context.peer())
            yield b"started"
            while context.is_active():
                time.sleep(0.05)
        self.server = grpc.server(ThreadPoolExecutor(8), handlers=[
            grpc.method_handlers_generic_handler(
                "t.T", {"Who": grpc.unary_unary_rpc_method_handler(who),
                        "Hang": grpc.unary_stream_rpc_method_handler(hang)})],
            options=list(options), maximum_concurrent_rpcs=max_rpcs)
        actual = self.server.add_insecure_port(self.front.backend)
        assert actual
        if not unix:
            self.front.set_backend_port(actual)
        self.server.start()
        self.front.start()
        self.port = self.front.port

    def channel(self):
        return self.grpc.insecure_channel(f"127.0.0.1:{self.port}",
                                          options=[("grpc.use_local_subchannel_pool", 1)])

    def who(self, channel) -> str:
        return channel.unary_unary("/t.T/Who")(b"", timeout=10).decode()

    def raw(self) -> socket.socket:
        return socket.create_connection(("127.0.0.1", self.port), timeout=10)

    def close(self) -> None:
        self.front.stop()
        self.server.stop(None).wait(5)
        self.front.cleanup()


def attempt(port: int) -> socket.socket | None:
    """A connection beyond a cap: None when the reset came during connect."""
    try:
        return socket.create_connection(("127.0.0.1", port), timeout=10)
    except (ConnectionResetError, ConnectionRefusedError):
        return None


def closed(s: socket.socket | None, timeout: float = 10.0) -> bool:
    """Whether the gateway closes or resets `s` within `timeout`."""
    if s is None:
        return True
    s.settimeout(timeout)
    try:
        while True:
            data = s.recv(4096)
            if not data:
                return True
    except (ConnectionResetError, BrokenPipeError):
        return True
    except socket.timeout:
        return False


def full(port: int) -> bool:
    """Whether a further connection of 127.0.0.1 is refused."""
    s = attempt(port)
    if closed(s, 0.5):
        return True
    s.close()
    return False


def h2_call(port: int, rcvbuf: int = 0) -> socket.socket:
    """A raw HTTP/2 client that sends one call of /t.T/Who with large
    windows (a small receive buffer when `rcvbuf`) and reads nothing."""
    import struct

    def frame(t, fl, sid, payload=b""):
        return struct.pack(">I", len(payload))[1:] + bytes([t, fl]) \
            + struct.pack(">I", sid) + payload

    def lit(n, v):
        n, v = n.encode(), v.encode()
        return b"\x00" + bytes([len(n)]) + n + bytes([len(v)]) + v
    hdr = b"".join(lit(*kv) for kv in [
        (":method", "POST"), (":scheme", "http"), (":path", "/t.T/Who"),
        (":authority", "x"), ("content-type", "application/grpc"), ("te", "trailers")])
    s = socket.socket()
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.connect(("127.0.0.1", port))
    s.sendall(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
              + frame(4, 0, 0, struct.pack(">HI", 4, 2**31 - 1))
              + frame(8, 0, 0, struct.pack(">I", 2**31 - 2**16 - 1))
              + frame(1, 4, 1, hdr) + frame(0, 1, 1, b"\x00" * 5))
    return s


SETTINGS = b"\x00\x00\x00\x04\x00\x00\x00\x00\x00"     # an empty SETTINGS frame


def test_keys():
    assert peer_key("192.0.2.7") == "192.0.2.7"
    assert peer_key("::ffff:192.0.2.7") == "192.0.2.7"
    assert peer_key("2001:db8:1:2:3::4") == "2001:db8:1:2::/64"
    assert prefix_key("192.0.2.7") == "192.0.2.0/24"
    assert prefix_key("::ffff:192.0.2.7") == "192.0.2.0/24"
    assert prefix_key("2001:db8:1:2:3::4") == "2001:db8:1::/48"


@pytest.mark.parametrize("unix", [True, False], ids=["unix", "loopback"])
def test_client_address_is_carried(grpc, unix):
    """The gRPC server sees the backend connection; the Front maps it back
    to the accepted client. Over loopback, a connection that does not come
    through the Front is not one of its peers."""
    if unix and not hasattr(socket, "AF_UNIX"):
        pytest.skip("no Unix sockets")
    b = Backend(grpc, unix=unix)
    try:
        chans = [b.channel() for _ in range(3)]
        assert [b.who(c) for c in chans] == ["127.0.0.1"] * 3
        assert b.front.total == 3
        if not unix:
            direct = grpc.insecure_channel(f"127.0.0.1:{b.front.backend_port}")
            assert b.who(direct) == "unknown"
            direct.close()
        for c in chans:
            c.close()
        wait_for(lambda: b.front.total == 0, "the connections to end")
        assert not b.front.peers
    finally:
        b.close()


def test_flood_from_one_address(grpc):
    """Beyond 4 connections of one address every new one is reset at once
    and recorded (`overloaded`, `connections`); the four places come back
    when their holders are closed at the deadline."""
    b = Backend(grpc, deadline=2.0, max_per_address=4)
    try:
        held = [b.raw() for _ in range(4)]
        wait_for(lambda: b.front.total == 4, "the four connections")
        t0 = time.monotonic()
        flood = [attempt(b.port) for _ in range(100)]
        assert all(closed(s, 5) for s in flood)
        assert time.monotonic() - t0 < 10
        wait_for(lambda: b.gw.count("overloaded", "connections") == 100, "the records")
        ch = b.channel()
        with pytest.raises(grpc.RpcError) as e:
            ch.unary_unary("/t.T/Who")(b"", timeout=1)
        assert e.value.code() in (grpc.StatusCode.UNAVAILABLE,
                                  grpc.StatusCode.DEADLINE_EXCEEDED)
        assert all(closed(s, 10) for s in held)
        wait_for(lambda: b.gw.count("read_timeout", "grpc handshake") == 4, "the records")
        wait_for(lambda: b.front.total == 0, "the places to come back")
        assert b.who(b.channel()) == "127.0.0.1"
        for s in held + [s for s in flood if s is not None]:
            s.close()
        ch.close()
    finally:
        b.close()


def test_caps_per_prefix_and_in_all(grpc):
    """With a large cap per address, the caps per prefix and in all
    still hold (here 127.0.0.1 stands for many addresses of one /24)."""
    for caps, n in (({"max_per_address": 100, "max_per_prefix": 3}, 3),
                    ({"max_per_address": 100, "max_connections": 5}, 5)):
        b = Backend(grpc, deadline=5.0, **caps)
        try:
            held = [b.raw() for _ in range(n)]
            wait_for(lambda: b.front.total == n, "the connections")
            extra = [attempt(b.port) for _ in range(10)]
            assert all(closed(s, 5) for s in extra)
            assert b.front.total == n
            wait_for(lambda: b.gw.count("overloaded", "connections") == 10, "the records")
            for s in held + [s for s in extra if s is not None]:
                s.close()
        finally:
            b.close()


def test_slowloris_and_half_closed(grpc):
    """A connection that sends its first bytes one at a time, one that
    sends nothing, and one that never sends a whole HTTP/2 preface are
    reset at the deadline; one that half-closes at once is closed at
    once. None of them reaches a call."""
    b = Backend(grpc, deadline=1.5)
    try:
        slow, silent, partial = b.raw(), b.raw(), b.raw()
        partial.sendall(b"PRI * HTTP/2.0\r\n")
        stop = threading.Event()

        def drip():
            for byte in b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n":
                if stop.wait(0.2):
                    return
                try:
                    slow.send(bytes([byte]))
                except OSError:
                    return
        threading.Thread(target=drip, daemon=True).start()
        half = b.raw()
        half.shutdown(socket.SHUT_WR)
        t0 = time.monotonic()
        assert closed(half, 1.0)
        assert all(closed(s, 5) for s in (slow, silent, partial))
        assert 1.0 < time.monotonic() - t0 < 10
        stop.set()
        wait_for(lambda: b.gw.count("read_timeout", "grpc handshake") == 3, "the records")
        wait_for(lambda: b.front.total == 0, "the places to come back")
        for s in (slow, silent, partial, half):
            s.close()
    finally:
        b.close()


def test_established_connection_outlives_the_deadline(grpc):
    """A connection that has carried a call is not closed at the deadline."""
    b = Backend(grpc, deadline=1.0)
    try:
        ch = b.channel()
        assert b.who(ch) == "127.0.0.1"
        time.sleep(2.0)
        assert b.front.total == 1
        assert b.who(ch) == "127.0.0.1"
        assert b.gw.got == []
        ch.close()
    finally:
        b.close()


@pytest.mark.parametrize("streaming", [False, True], ids=["idle", "streaming"])
def test_half_open_after_a_call(grpc, streaming):
    """A client that vanishes after a call (here a relay that stops passing
    bytes without closing) loses its connection: without a call by the
    gRPC server's idle limit, and with a call open (a stream) when its
    keepalive ping is not answered; the Front then closes its side and the
    place comes back. The gateway uses 120 s, and 60 s and 20 s; this
    test 1 s and 0.5 s (grpcio 1.85 times the answer to a keepalive ping
    with `grpc.http2.ping_timeout_ms`)."""
    b = Backend(grpc, deadline=30.0, options=[("grpc.max_connection_idle_ms", 1000),
                                              ("grpc.keepalive_time_ms", 500),
                                              ("grpc.keepalive_timeout_ms", 500),
                                              ("grpc.http2.ping_timeout_ms", 500)])
    relay = socket.socket()
    relay.bind(("127.0.0.1", 0))
    relay.listen(1)
    frozen = threading.Event()
    keep = []

    def pump(a, c):
        try:
            while True:
                data = a.recv(65536)
                if not data or frozen.is_set():
                    return
                c.sendall(data)
        except OSError:
            return

    def serve():
        a, _ = relay.accept()
        c = socket.create_connection(("127.0.0.1", b.port))
        keep.extend((a, c))
        threading.Thread(target=pump, args=(a, c), daemon=True).start()
        threading.Thread(target=pump, args=(c, a), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    try:
        ch = grpc.insecure_channel(f"127.0.0.1:{relay.getsockname()[1]}")
        assert b.who(ch) == "127.0.0.1"
        if streaming:
            it = ch.unary_stream("/t.T/Hang")(b"", timeout=60)
            assert next(it) == b"started"
        assert b.front.total == 1
        frozen.set()
        wait_for(lambda: b.front.total == 0, "the dead connection to end", timeout=20)
        if streaming:
            it.cancel()
        ch.close()
    finally:
        for s in keep + [relay]:
            s.close()
        b.close()


def test_client_that_stops_reading_floods_settings(grpc):
    """Review H1: after one call, a client sends empty SETTINGS frames and
    reads none of the ACKs. The place comes back: either gRPC closes the
    connection (which the Front then sees) or, once more than PENDING
    bytes wait and none goes out within the drain deadline (here 2 s),
    both sides are reset and recorded (`grpc stalled`)."""
    b = Backend(grpc, deadline=30.0, max_per_address=1, drain_timeout=2.0)
    try:
        s = h2_call(b.port, rcvbuf=2048)
        wait_for(lambda: any(c.called for c in list(b.front.peers.values())), "the call")
        s.setblocking(False)
        blob = SETTINGS * 1000
        t0 = time.monotonic()
        while b.front.total and time.monotonic() - t0 < 20:
            try:
                s.send(blob)
            except BlockingIOError:
                time.sleep(0.02)
            except OSError:
                break
        wait_for(lambda: b.front.total == 0, "the place to come back", timeout=20)
        assert b.gw.count("read_timeout", "grpc stalled") <= 1
        assert b.who(b.channel()) == "127.0.0.1"
        s.close()
    finally:
        b.close()


def test_stalled_client_is_reset():
    """Review H1, deterministic: a server that sends 8 MiB and never
    closes, a client that never reads. Once more than PENDING bytes wait
    and nothing goes out within the drain deadline (here 1 s), both sides
    are reset and recorded (`grpc stalled`)."""
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("no Unix sockets")
    gw = Recorder()
    f = Front(gw, ("127.0.0.1", 0), deadline=30.0, drain_timeout=1.0)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(f.path)
    srv.listen(8)
    held = []

    def backend():
        c, _ = srv.accept()
        held.append(c)
        peer = "unix:" + c.getpeername()
        while f.client(peer) is None:
            time.sleep(0.01)
        try:
            c.settimeout(10)
            c.sendall(b"x" * (8 << 20))
        except OSError:
            pass
    threading.Thread(target=backend, daemon=True).start()
    f.start()
    try:
        cl = socket.socket()
        cl.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        cl.connect(("127.0.0.1", f.port))
        wait_for(lambda: f.total == 1, "the connection")
        wait_for(lambda: f.total == 0, "the reset", timeout=20)
        wait_for(lambda: gw.count("read_timeout", "grpc stalled") == 1, "the record")
        cl.close()
    finally:
        f.stop()
        for c in held:
            c.close()
        srv.close()
        f.cleanup()


def test_server_close_seen_while_client_does_not_read(grpc):
    """Review H1: a client that stops reading after its call, with less
    than PENDING bytes waiting for it: when the gRPC server closes the idle connection (here
    after 1 s), the Front sees it while the client still reads nothing,
    and resets the client's side after CLOSE_GRACE."""
    from cadl.a2a_grpc_front import CLOSE_GRACE
    b = Backend(grpc, deadline=30.0, options=[("grpc.max_connection_idle_ms", 1000)])
    try:
        s = h2_call(b.port, rcvbuf=2048)
        wait_for(lambda: any(c.called for c in list(b.front.peers.values())), "the call")
        # About 140 KiB of SETTINGS ACKs: more than the transport keeps
        # before it waits for the client, less than PENDING.
        s.sendall(SETTINGS * 16000)
        t0 = time.monotonic()
        wait_for(lambda: b.front.total == 0, "the place to come back",
                 timeout=CLOSE_GRACE + 15)
        assert time.monotonic() - t0 < CLOSE_GRACE + 10
        assert not b.front.peers
        assert closed(s, 5)
        s.close()
    finally:
        b.close()


def test_start_failure_is_raised(monkeypatch):
    """Review L2: a front listener that cannot start says so at once."""
    import asyncio

    async def broken(*a, **k):
        raise OSError("no server")
    monkeypatch.setattr(asyncio, "start_server", broken)
    f = Front(Recorder(), ("127.0.0.1", 0))
    try:
        t0 = time.monotonic()
        with pytest.raises(OSError, match="no server"):
            f.start()
        assert time.monotonic() - t0 < 5
    finally:
        f.stop()
        f.cleanup()


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


def test_localhost_binds_every_address():
    """Review L6: `localhost` is listened on at each of its addresses, on
    one port, as gRPC did."""
    if not _ipv6_loopback():
        pytest.skip("no IPv6 loopback")
    infos = {i[4][0] for i in socket.getaddrinfo("localhost", 0, type=socket.SOCK_STREAM)}
    f = Front(Recorder(), ("localhost", 0))
    try:
        assert {s.getsockname()[0] for s in f.socks} == infos
        assert {s.getsockname()[1] for s in f.socks} == {f.port}
    finally:
        f.stop()
        f.cleanup()


def test_unbindable_address_is_left_out(monkeypatch, capsys):
    """Review L-b: an address of the host that cannot be bound (::1 where
    IPv6 is off) is logged and left out while another one is bound; with
    port 0, a port that a later address cannot take is given up for
    another one."""
    import errno

    from cadl import a2a_grpc_front as F
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
             (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))]
    monkeypatch.setattr(F.socket, "getaddrinfo", lambda *a, **k: infos)
    real = F._listen_socket

    def off(family, addr):
        if family == socket.AF_INET6:
            raise OSError(errno.EADDRNOTAVAIL, "Can't assign requested address")
        return real(family, addr)
    monkeypatch.setattr(F, "_listen_socket", off)
    socks = F._listen_sockets("localhost", 0)
    assert [s.family for s in socks] == [socket.AF_INET]
    assert "::1" in capsys.readouterr().err
    for s in socks:
        s.close()

    busy = iter([True, False])

    def collide(family, addr):
        if family == socket.AF_INET6 and next(busy):
            raise OSError(errno.EADDRINUSE, "Address already in use")
        if family == socket.AF_INET6:
            family, addr = socket.AF_INET, ("127.0.0.1", 0)    # stands in for ::1
        return real(family, addr)
    monkeypatch.setattr(F, "_listen_socket", collide)
    socks = F._listen_sockets("localhost", 0)
    assert len(socks) == 2          # the second attempt bound both
    for s in socks:
        s.close()
    with pytest.raises(OSError):
        monkeypatch.setattr(F, "_listen_socket",
                            lambda f, a: (_ for _ in ()).throw(OSError(errno.EACCES, "no")))
        F._listen_sockets("localhost", 0)


def test_calls_refused_by_grpc_count(grpc):
    """A call that gRPC refuses before a handler runs, for an unknown
    method (UNIMPLEMENTED) or beyond its limit of concurrent calls
    (RESOURCE_EXHAUSTED), counts as a call of its connection: the
    connection is not reset at the deadline. A connection without a call
    still is."""
    b = Backend(grpc, deadline=1.5, max_rpcs=1)
    try:
        silent = b.raw()
        unknown = b.channel()
        with pytest.raises(grpc.RpcError) as e:
            unknown.unary_unary("/t.T/Nope")(b"", timeout=10)
        assert e.value.code() == grpc.StatusCode.UNIMPLEMENTED
        holder = b.channel()
        stream = holder.unary_stream("/t.T/Hang")(b"", timeout=30)
        assert next(stream) == b"started"
        busy = b.channel()
        with pytest.raises(grpc.RpcError) as e:
            busy.unary_unary("/t.T/Who")(b"", timeout=10)
        assert e.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        assert closed(silent, 15)
        time.sleep(1.5)
        assert b.front.total == 3
        assert b.gw.got == [("read_timeout", "grpc handshake")]
        stream.cancel()

        def served():
            try:
                return b.who(busy)
            except grpc.RpcError:       # the stream's worker is not free yet
                return None
        assert wait_for(served, "the worker") == "127.0.0.1" and b.front.total == 3
        for c in (unknown, holder, busy):
            c.close()
        silent.close()
    finally:
        b.close()


def test_connection_age(grpc):
    """gRPC sends GOAWAY at the age of a connection (here 1 s, with a
    jitter of 10 %) and closes it after the grace (1 s): a stream that
    lasts longer ends with UNAVAILABLE, a unary call under way ends
    normally, the Front closes its side and the place comes back; the
    client's next call goes on a new connection. Nothing is recorded."""
    b = Backend(grpc, deadline=5.0, max_age=1.0, age_grace=1.0,
                options=[("grpc.max_connection_age_ms", 1000),
                         ("grpc.max_connection_age_grace_ms", 1000)])
    try:
        ch = b.channel()
        t0 = time.monotonic()
        assert b.who(ch) == "127.0.0.1"
        stream = ch.unary_stream("/t.T/Hang")(b"", timeout=60)
        assert next(stream) == b"started"
        with pytest.raises(grpc.RpcError) as e:
            for _ in stream:
                pass
        assert e.value.code() == grpc.StatusCode.UNAVAILABLE
        assert time.monotonic() - t0 < 15
        wait_for(lambda: b.front.total == 0, "the place to come back")
        assert b.who(ch) == "127.0.0.1"
        assert b.front.total == 1
        ch.close()
        assert b.gw.got == []
    finally:
        b.close()


def test_connection_kept_past_its_age_is_reset(grpc, monkeypatch):
    """When gRPC keeps a connection past its age and grace (here a gRPC
    server without the age), the Front resets it at `Front.lifetime` and
    records it (`read_timeout`, `grpc age`)."""
    monkeypatch.setattr(F, "AGE_MARGIN", 0.5)
    b = Backend(grpc, deadline=1.0, max_age=1.0, age_grace=0.5)
    try:
        assert b.front.lifetime() == pytest.approx(1.0 + 1.1 + 0.5 + 0.5)
        ch = b.channel()
        assert b.who(ch) == "127.0.0.1"
        stream = ch.unary_stream("/t.T/Hang")(b"", timeout=60)
        assert next(stream) == b"started"
        with pytest.raises(grpc.RpcError) as e:
            for _ in stream:
                pass
        assert e.value.code() == grpc.StatusCode.UNAVAILABLE
        wait_for(lambda: b.gw.count("read_timeout", "grpc age") == 1, "the record")
        ch.close()
    finally:
        b.close()


def test_waiting_place_goes_to_the_fewest(grpc):
    """With every place taken (3 here, all of 127.0.0.1), a further
    connection waits; when a place comes back it goes to the waiting
    connection of ::1, which holds none, before the one of 127.0.0.1
    that came first; that one is reset at the end of its wait and
    recorded. A waiting connection counts against the cap of its
    address."""
    try:
        probe = socket.socket(socket.AF_INET6)
        probe.bind(("::1", 0))
        probe.close()
    except OSError:
        pytest.skip("no IPv6 loopback")
    b = Backend(grpc, deadline=30.0, listen="::", max_connections=3, max_per_address=4,
                wait=4.0)
    try:
        held = [b.raw() for _ in range(3)]
        wait_for(lambda: b.front.total == 3, "the three connections")
        v4 = b.raw()
        wait_for(lambda: len(b.front.waiters) == 1, "the first to wait")
        beyond = attempt(b.port)              # 3 held + 1 waiting: the address is full
        assert closed(beyond, 5)
        v6 = socket.create_connection(("::1", b.port), timeout=10)
        wait_for(lambda: len(b.front.waiters) == 2, "the second to wait")
        held[0].close()
        wait_for(lambda: b.front.address_count.get("::/64") == 1, "::1 to get the place")
        assert b.front.address_count["127.0.0.1"] == 2
        t0 = time.monotonic()
        assert closed(v4, 15)
        assert time.monotonic() - t0 < 10
        wait_for(lambda: b.gw.count("overloaded", "connections") == 2, "the records")
        assert not b.front.waiters and b.front.total == 3
        for s in held + [v4, v6] + ([beyond] if beyond is not None else []):
            s.close()
    finally:
        b.close()


def test_waiting_connection_called_after_its_place(grpc):
    """A gRPC client that waits for a place is served once it has one;
    its deadline to the first call starts then."""
    b = Backend(grpc, deadline=2.0, max_connections=1, max_per_address=4, wait=8.0)
    try:
        held = b.raw()
        wait_for(lambda: b.front.total == 1, "the connection")
        got: list = []
        t = threading.Thread(target=lambda: got.append(b.who(b.channel())), daemon=True)
        t.start()
        wait_for(lambda: len(b.front.waiters) == 1, "the client to wait")
        assert closed(held, 10)                 # reset at its deadline
        t.join(15)
        assert got == ["127.0.0.1"]
        assert b.gw.got == [("read_timeout", "grpc handshake")]
        held.close()
    finally:
        b.close()


# === Through the gateway ===========================================================

def _run(tmp_path, *extra, listen="127.0.0.1:0"):
    pf = tmp_path / "grpc-port"
    r = Run(tmp_path, extra=("--grpc-listen", listen, "--grpc-port-file", str(pf), *extra))
    wait_for(lambda: pf.exists() and pf.read_text().strip(), "the port")
    return r, int(pf.read_text())


def test_gateway_connection_flood(tmp_path, grpc, capsys):
    """Through the gateway: one address holds its 4 connections with
    nothing sent; a flood of 300 more is reset and recorded at the capped
    rate; a call of the same address is refused until the deadline resets
    the four, then served; the replay is byte-identical."""
    pb = pytest.importorskip("a2a.types.a2a_pb2")
    r, port = _run(tmp_path, "--read-timeout", "6")
    held = [socket.create_connection(("127.0.0.1", port)) for _ in range(4)]
    wait_for(lambda: full(port), "the four connections to be counted")
    flood = [attempt(port) for _ in range(300)]
    assert all(closed(s, 5) for s in flood)
    g = GrpcClient(r, grpc, pb, port)
    with pytest.raises(grpc.RpcError):
        g.channel.unary_unary("/lf.a2a.v1.A2AService/SendMessage")(b"", timeout=1)
    assert all(closed(s, 15) for s in held)
    g.channel.close()
    g = GrpcClient(r, grpc, pb, port)
    got = wait_for(lambda: _try(g, pb), "a call after the deadline")
    assert got.task.id == "up-task-1"
    g.channel.close()
    for s in held + [s for s in flood if s is not None]:
        s.close()
    r.stop()
    refused = [why for _, why in events(r.audit, "overloaded")]
    assert refused and all(w.split(";")[0] == "connections" for w in refused)
    # At most 20 records a second; the next record counts those left out
    # (of the 300 and the attempts of the refused channel; those of the
    # last second stay uncounted).
    left_out = sum(int(w.split("=")[1]) for w in refused if ";" in w)
    assert 20 <= len(refused) < 100 and len(refused) + left_out <= 320
    assert [why for _, why in events(r.audit, "read_timeout")] == ["grpc handshake"] * 4
    assert r.agent.sent() == ["summarize"]
    r.replay_check()
    capsys.readouterr()


def _try(g, pb):
    try:
        return g.call("PLANNER", "SendMessage", grpc_message(pb, "summarize"),
                      pb.SendMessageResponse)
    except Exception:       # noqa: BLE001 - the places are not back yet
        return None


def test_gateway_tls_call_served_while_an_address_holds_its_connections(
        tmp_path, grpc, pki, capsys):
    """Mutual TLS through the front listener on [::] (IPv4 and IPv6):
    127.0.0.1 holds its 4 connections, two never starting TLS, one
    sending a ClientHello a byte at a time, one finishing TLS without a
    call; a client of ::1 with its certificate is served meanwhile, and
    the actor of its certificate is still checked. The four are reset at
    the deadline."""
    pb = pytest.importorskip("a2a.types.a2a_pb2")
    try:
        probe = socket.socket(socket.AF_INET6)
        probe.bind(("::1", 0))
        probe.close()
    except OSError:
        pytest.skip("no IPv6 loopback")
    r, port = _run(tmp_path, *pki.gateway_args(), "--read-timeout", "8",
                   listen="[::]:0")
    held = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
    ctx = pki.context("planner")
    ctx.set_alpn_protocols(["h2"])
    tls = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port)),
                          server_hostname="localhost")
    held.append(tls)
    stop = threading.Event()

    def drip():
        for byte in b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03":
            if stop.wait(0.3):
                return
            try:
                held[0].send(bytes([byte]))
            except OSError:
                return
    threading.Thread(target=drip, daemon=True).start()
    flood = [attempt(port) for _ in range(20)]
    assert all(closed(s, 5) for s in flood)
    key = (pki.dir / "planner.key").read_bytes()
    chain = (pki.dir / "planner.pem").read_bytes()
    creds = grpc.ssl_channel_credentials(pki.ca.read_bytes(), key, chain)
    g = GrpcClient(r, grpc, pb, port)
    g.channel.close()
    g.channel = grpc.secure_channel(f"[::1]:{port}", creds, options=[
        ("grpc.ssl_target_name_override", "localhost")])
    got = g.call("PLANNER", "SendMessage", grpc_message(pb, "summarize"),
                 pb.SendMessageResponse)
    assert got.task.id == "up-task-1"
    assert g.error(g.call, "OPERATOR", "SendMessage", grpc_message(pb, "summarize"),
                   pb.SendMessageResponse)[0] == "UNAUTHENTICATED"
    # Served while the four are still open.
    assert not closed(held[1], 0.05) and not closed(held[2], 0.05)
    v4 = grpc.secure_channel(f"127.0.0.1:{port}", creds,
                             options=[("grpc.use_local_subchannel_pool", 1)])
    with pytest.raises(grpc.RpcError):
        v4.unary_unary("/lf.a2a.v1.A2AService/SendMessage")(b"", timeout=1)
    v4.close()
    assert all(closed(s, 15) for s in held)
    stop.set()
    g.channel.close()
    for s in held + [s for s in flood if s is not None]:
        s.close()
    r.stop()
    assert [why for _, why in events(r.audit, "read_timeout")] == ["grpc handshake"] * 4
    assert [why for _, why in events(r.audit, "overloaded")][0] == "connections"
    assert r.agent.sent() == ["summarize"]
    r.replay_check()
    capsys.readouterr()


def test_gateway_tls_unknown_method_counts_and_age(tmp_path, grpc, pki, capsys):
    """Mutual TLS through the gateway: a connection whose only call is of
    an unknown method (refused by gRPC with UNIMPLEMENTED) is not reset at
    the deadline. With an age of 2 s and a grace of 1 s, a client keeps
    calling across its connections' ends. Nothing is recorded as
    `read_timeout`."""
    pb = pytest.importorskip("a2a.types.a2a_pb2")
    r, port = _run(tmp_path, *pki.gateway_args(), "--read-timeout", "3",
                   "--grpc-max-connection-age", "2", "--grpc-max-connection-age-grace", "1")
    key = (pki.dir / "planner.key").read_bytes()
    chain = (pki.dir / "planner.pem").read_bytes()
    creds = grpc.ssl_channel_credentials(pki.ca.read_bytes(), key, chain)
    unknown = grpc.secure_channel(f"127.0.0.1:{port}", creds, options=[
        ("grpc.ssl_target_name_override", "localhost"),
        ("grpc.use_local_subchannel_pool", 1)])
    with pytest.raises(grpc.RpcError) as e:
        unknown.unary_unary("/lf.a2a.v1.A2AService/Nope")(b"", timeout=10)
    assert e.value.code() == grpc.StatusCode.UNIMPLEMENTED
    g = GrpcClient(r, grpc, pb, port)
    g.channel.close()
    g.channel = grpc.secure_channel(f"127.0.0.1:{port}", creds, options=[
        ("grpc.ssl_target_name_override", "localhost")])
    for i in range(3):
        got = g.call("PLANNER", "SendMessage", grpc_message(pb, "summarize"),
                     pb.SendMessageResponse)
        assert got.task.id == f"up-task-{i + 1}"
        time.sleep(2.5)
    unknown.close()
    g.channel.close()
    r.stop()
    assert events(r.audit, "read_timeout") == []
    assert r.agent.sent() == ["summarize"] * 3
    r.replay_check()
    capsys.readouterr()


def test_gateway_options_must_be_positive(tmp_path, capsys):
    from cadl.cli import main

    from .test_a2a_gateway import CADL, CLIENT_KEYS, _ring
    assert main(["a2a-gateway", str(CADL), "--agent-actor", "SUMMARIZER", "--upstream",
                 "http://127.0.0.1:9", "--client-keys", str(_ring(tmp_path, CLIENT_KEYS,
                                                                  "clients")),
                 "--audit", str(tmp_path / "a.jsonl"),
                 "--grpc-listen", "127.0.0.1:0", "--max-grpc-connections-per-address",
                 "0"]) == 2
    assert "must be positive" in capsys.readouterr().err
    for option, value in (("--grpc-max-connection-age", "0"),
                          ("--grpc-max-connection-age-grace", "-1"),
                          ("--grpc-connection-wait", "-1")):
        assert main(["a2a-gateway", str(CADL), "--agent-actor", "SUMMARIZER", "--upstream",
                     "http://127.0.0.1:9", "--client-keys",
                     str(tmp_path / "clients.json"), "--audit", str(tmp_path / "a.jsonl"),
                     "--grpc-listen", "127.0.0.1:0", option, value]) == 2
        assert "--grpc-max-connection-age must be positive" in capsys.readouterr().err
