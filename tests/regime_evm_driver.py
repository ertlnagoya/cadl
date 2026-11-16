"""Runs the Solidity code that `cadl codegen -t solidity` generates for a
file with regime bindings on an EVM, for tests/test_regime_runtime.py.

This script does not import cadl: it runs in the Python of
CADL_EVM_PYTHON (eth-tester[py-evm]), as tests/norms_evm_driver.py does.

    python regime_evm_driver.py JOB.json OUT.json

JOB.json: {"contracts": {name: {"abi": [...], "bin": "..."}}, "main": name,
"steps": [step, ...]}. The main contract is deployed (its constructor
deploys the others). A step is ["call", path, function, args] (a view
call) or ["send", path, function, args] (a transaction): path is a list of
getters of the main contract that lead to the contract to call ([] is the
main contract, ["routing"] the contract its getter `routing` returns), and
each getter's contract is found by the name of its return type.
OUT.json: one entry per step: the decoded outputs of a call, or "ok" or
{"error": text} for a transaction.
"""

from __future__ import annotations

import json
import sys

from eth_abi import decode, encode
from eth_tester import EthereumTester, PyEVMBackend
from eth_utils import keccak

GAS = 25_000_000


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as f:
        job = json.load(f)
    contracts = job["contracts"]
    params = PyEVMBackend.generate_genesis_params(overrides={"gas_limit": 30_000_000})
    t = EthereumTester(PyEVMBackend(genesis_parameters=params))
    acct = t.get_accounts()[0]
    tx = t.send_transaction({"from": acct, "data": "0x" + contracts[job["main"]]["bin"],
                             "gas": GAS})
    main_address = t.get_transaction_receipt(tx)["contract_address"]

    def fn(name: str, function: str) -> dict:
        for x in contracts[name]["abi"]:
            if x.get("type") == "function" and x["name"] == function:
                return x
        raise KeyError(f"{name} has no function {function}")

    def types(xs: list) -> list:
        return [x["type"] if x["type"] != "contract" else "address" for x in xs]

    def call(name: str, address: str, function: str, args: list, send: bool):
        f = fn(name, function)
        ins = types(f["inputs"])
        data = keccak(text=f"{function}({','.join(ins)})")[:4] + encode(ins, args)
        tx = {"from": acct, "to": address, "data": "0x" + data.hex(), "gas": GAS}
        if send:
            try:
                h = t.send_transaction(tx)
            except Exception as e:      # reverted
                return {"error": str(e)[:300]}
            return "ok" if t.get_transaction_receipt(h).get("status", 1) else {"error": "reverted"}
        raw = t.call(tx)
        raw = bytes.fromhex(raw[2:]) if isinstance(raw, str) else bytes(raw)
        outs = [o["type"] for o in f["outputs"]]
        outs = ["address" if o.startswith("contract") else o for o in outs]
        return list(decode(outs, raw))

    out = []
    for kind, path, function, args in job["steps"]:
        name, address = job["main"], main_address
        for getter in path:
            f = fn(name, getter)
            address = call(name, address, getter, [], False)[0]
            internal = f["outputs"][0].get("internalType", "")
            name = internal.split()[-1] if internal.startswith("contract ") else name
        out.append(call(name, address, function, args, kind == "send"))
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(out, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
