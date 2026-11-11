"""Runs the norms monitor of the Solidity target on an EVM, for
tests/test_norms_monitor_targets.py.

This script does not import cadl: it runs in a separate Python
environment that has an EVM from PyPI (``pip install "eth-tester[py-evm]"``),
named by the environment variable CADL_EVM_PYTHON of the tests.

    python norms_evm_driver.py JOB.json OUT.json

JOB.json: {"contracts": {name: {"abi": [...], "bin": "..."}}, "libraries":
[names in the order of deployment], "monitor": name, "runs": [run, ...]}.
A run is {"begin": [bindings, request, quantities] or null, "steps":
[{"observe": [t, values, messages, hasState, state, end]} or {"finish":
true}, ...]}; values and arguments are [name, kind, n, s].
OUT.json: per run, per step: {"error": text} for a reverted call, or
{"verdicts": [states, verdicts, assumptions, guarantees], "logs": [...]}.
"""

from __future__ import annotations

import json
import sys

from eth_abi import decode, encode
from eth_tester import EthereumTester, PyEVMBackend
from eth_utils import keccak

GAS = 25_000_000


def _types(abi_inputs: list) -> list[str]:
    out = []
    for x in abi_inputs:
        t = x["type"]
        if t.startswith("tuple"):
            t = "(" + ",".join(_types(x["components"])) + ")" + t[len("tuple"):]
        out.append(t)
    return out


class Chain:
    def __init__(self) -> None:
        params = PyEVMBackend.generate_genesis_params(overrides={"gas_limit": 30_000_000})
        self.t = EthereumTester(PyEVMBackend(genesis_parameters=params))
        self.acct = self.t.get_accounts()[0]

    def deploy(self, code: str) -> str:
        tx = self.t.send_transaction({"from": self.acct, "data": "0x" + code, "gas": GAS})
        receipt = self.t.get_transaction_receipt(tx)
        if not receipt["contract_address"] or receipt.get("status") == 0:
            raise RuntimeError("deployment failed")
        return receipt["contract_address"]


def _bytes(x) -> bytes:
    if isinstance(x, str):
        return bytes.fromhex(x[2:] if x.startswith("0x") else x)
    return bytes(x)


def link(code: str, name: str, address: str) -> str:
    placeholder = "__$" + keccak(text=name).hex()[:34] + "$__"
    return code.replace(placeholder, address[2:].lower())


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as f:
        job = json.load(f)
    contracts = job["contracts"]
    abi = contracts[job["monitor"]]["abi"]
    fns = {x["name"]: x for x in abi if x.get("type") == "function"}
    events = {}
    for x in abi:
        if x.get("type") == "event":
            sig = f"{x['name']}({','.join(_types(x['inputs']))})"
            events[keccak(text=sig)] = (x["name"], _types(x["inputs"]))

    def selector(name: str) -> tuple[bytes, list[str]]:
        types = _types(fns[name]["inputs"])
        return keccak(text=f"{name}({','.join(types)})")[:4], types

    out = []
    chain = Chain()
    addresses: dict = {}
    for name in job["libraries"]:
        c = contracts[name]["bin"]
        for other, addr in addresses.items():
            c = link(c, other, addr)
        addresses[name] = chain.deploy(c)
    code = contracts[job["monitor"]]["bin"]
    for other, addr in addresses.items():
        code = link(code, other, addr)
    # one deployment; each run starts from a snapshot of the chain taken after it
    address = chain.deploy(code)
    t = chain.t
    fresh = t.take_snapshot()
    for run in job["runs"]:
        t.revert_to_snapshot(fresh)

        def call(name: str, args: list, transact: bool):
            sel, types = selector(name)
            data = "0x" + (sel + encode(types, args)).hex()
            tx = {"from": chain.acct, "to": address, "data": data, "gas": GAS}
            if transact:
                try:
                    h = t.send_transaction(tx)
                except Exception as e:          # the call reverted
                    return {"error": str(e)[:300]}
                receipt = t.get_transaction_receipt(h)
                if receipt.get("status") == 0:     # reverted: ask a call for the reason
                    try:
                        t.call(tx)
                    except Exception as e:
                        return {"error": str(e)[:300]}
                    return {"error": "reverted"}
                logs = []
                for log in receipt["logs"]:
                    topic = _bytes(log["topics"][0])
                    if topic in events:
                        ename, etypes = events[topic]
                        vals = decode(etypes, _bytes(log["data"]))
                        logs.append([ename, [v.hex() if isinstance(v, bytes) else v for v in vals]])
                return {"logs": logs, "gas": receipt["gas_used"]}
            raw = t.call(tx)
            raw = _bytes(raw)
            outs = _types(fns[name]["outputs"])
            return [list(v) if isinstance(v, tuple) else v for v in decode(outs, raw)]

        results = []
        if run.get("begin") is not None:
            r = call("begin", run["begin"], True)
            if "error" in r:
                results.append(r)
        for st in run["steps"]:
            if "finish" in st:
                r = call("finish", [], True)
            else:
                r = call("observe", st["observe"], True)
            if "error" not in r:
                r["verdicts"] = call("verdicts", [], False)
                r["ended"] = call("ended", [], False)[0]
            results.append(r)
        out.append(results)
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(out, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
