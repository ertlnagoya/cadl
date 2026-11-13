"""Certificates for the tests of mutual TLS at `cadl a2a-gateway`: a test
CA, a server certificate for 127.0.0.1, client certificates whose subject
common name is an actor, and a client certificate of another CA. Made with
the `openssl` command (EC P-256 keys, valid for two days); the tests that
need them are skipped without it."""

from __future__ import annotations

import secrets
import shutil
import ssl
import subprocess
from pathlib import Path

import pytest

_REQ = """[req]
distinguished_name=dn
[dn]
[ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
[server]
basicConstraints=CA:FALSE
subjectAltName=IP:127.0.0.1,DNS:localhost
extendedKeyUsage=serverAuth
[client]
basicConstraints=CA:FALSE
extendedKeyUsage=clientAuth
"""


def _openssl(*args: str, cwd: Path) -> None:
    done = subprocess.run(["openssl", *args], cwd=cwd, capture_output=True)
    if done.returncode != 0:
        pytest.skip(f"openssl cannot make a certificate: {done.stderr.decode()[:200]}")


def _key_and_csr(d: Path, name: str, cn: str) -> None:
    _openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
             "-pkeyopt", "ec_param_enc:named_curve", "-nodes",
             "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={cn}",
             "-config", "req.cnf", cwd=d)


def _ca(d: Path, name: str) -> None:
    _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
             "-pkeyopt", "ec_param_enc:named_curve",
             "-nodes", "-sha256", "-keyout", f"{name}.key", "-out", f"{name}.pem", "-days", "2",
             "-subj", f"/CN=CADL test {name}", "-config", "req.cnf", "-extensions", "ca",
             cwd=d)


def _sign(d: Path, name: str, ca: str, kind: str) -> None:
    _openssl("x509", "-req", "-in", f"{name}.csr", "-CA", f"{ca}.pem", "-CAkey", f"{ca}.key",
             "-set_serial", str(secrets.randbits(63)), "-out", f"{name}.pem", "-days", "2",
             "-sha256", "-extfile", "req.cnf", "-extensions", kind, cwd=d)


class PKI:
    """`ca.pem`, `server.pem`/`server.key`, and `<name>.pem`/`<name>.key`
    for every client certificate."""

    def __init__(self, d: Path, clients: dict[str, str], foreign: dict[str, str]):
        if shutil.which("openssl") is None:
            pytest.skip("openssl is not installed")
        self.dir = d
        d.mkdir(parents=True, exist_ok=True)
        (d / "req.cnf").write_text(_REQ)
        _ca(d, "ca")
        _ca(d, "other-ca")
        _key_and_csr(d, "server", "127.0.0.1")
        _sign(d, "server", "ca", "server")
        for name, cn in clients.items():
            _key_and_csr(d, name, cn)
            _sign(d, name, "ca", "client")
        for name, cn in foreign.items():
            _key_and_csr(d, name, cn)
            _sign(d, name, "other-ca", "client")
        self.ca = d / "ca.pem"
        self.cert = d / "server.pem"
        self.key = d / "server.key"

    def gateway_args(self) -> tuple:
        return ("--tls-cert", str(self.cert), "--tls-key", str(self.key),
                "--client-ca", str(self.ca))

    def context(self, client: str | None) -> ssl.SSLContext:
        """A client context that trusts the test CA and presents the
        certificate `client` (none for None)."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=str(self.ca))
        if client is not None:
            ctx.load_cert_chain(self.dir / f"{client}.pem", self.dir / f"{client}.key")
        return ctx
