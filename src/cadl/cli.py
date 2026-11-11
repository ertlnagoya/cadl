"""CADL Command-Line Interface."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cadl",
        description="CADL: Contract Architecture Description Language toolchain",
    )
    parser.add_argument("--version", action="version", version=f"cadl {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # parse command
    parse_cmd = subparsers.add_parser("parse", help="Parse a CADL file and report errors")
    parse_cmd.add_argument("file", type=Path, help="CADL file to parse")
    parse_cmd.add_argument("--ast", action="store_true", help="Print AST")

    # check command
    check_cmd = subparsers.add_parser("check", help="Parse and type-check a CADL file")
    check_cmd.add_argument("file", type=Path, help="CADL file to check")

    # verify command
    verify_cmd = subparsers.add_parser("verify", help="Parse, type-check, and verify a CADL file")
    verify_cmd.add_argument("file", type=Path, help="CADL file to verify")
    verify_cmd.add_argument("--format", choices=["text", "json"], default="text",
                             help="Output format (default: text)")

    # codegen command
    codegen_cmd = subparsers.add_parser("codegen", help="Generate runtime code from a CADL file")
    codegen_cmd.add_argument("file", type=Path, help="CADL file to generate from")
    codegen_cmd.add_argument("--target", "-t",
                             choices=["python", "solidity", "opa", "unity-csharp",
                                      "agent-rego", "agent-cedar"],
                             default="python",
                             help="Code generation target (default: python); "
                                  "agent-rego and agent-cedar generate the boundary "
                                  "policy of the agent governance extension "
                                  "(agents 0.1)")
    codegen_cmd.add_argument("--output", "-o", type=Path, default=Path("generated"),
                             help="Output directory (default: generated/)")

    # agents-decide command
    decide_cmd = subparsers.add_parser(
        "agents-decide",
        help="Decide one message at the boundary: allow, deny or escalate "
             "(agent governance extension, agents 0.1)",
        description="Reference decision function of the agent governance "
                    "extension (Appendix G, G.5) for one message. The policy input "
                    "(G.10) has 'message' (from, to, name) and optionally 'now' and "
                    "'instances' (bindings, in_force, derived). Stateless: holds, "
                    "grants and revocations belong to a host runtime. Exit status: "
                    "0 allow, 1 deny, 3 escalate, 2 usage error.")
    decide_cmd.add_argument("file", type=Path, help="CADL file with enforced contracts")
    decide_cmd.add_argument("--input", "-i", type=Path, required=True,
                            help="Policy input as a JSON file ('-' for stdin)")
    decide_cmd.add_argument("--format", choices=["text", "json"], default="text",
                            help="Output format (default: text)")

    # agents-run command
    run_cmd = subparsers.add_parser(
        "agents-run",
        help="Replay a recorded sequence of messages through the enforcement "
             "point: decisions, holds, delegations and audit records "
             "(agent governance extension, agents 0.1)",
        description="Reference runtime of the agent governance extension "
                    "(Appendix G, G.5 to G.8). It decides every message of the "
                    "trace with the decision function of `agents-decide`, on the "
                    "state left by the earlier messages: in-force flags from the "
                    "norms engine, derived instances from grants and revocations, "
                    "holds with approval, rejection and timeout. Deterministic "
                    "and offline: the only clock is 't' of the trace. Exit "
                    "status: 0 when the trace was replayed, 2 usage error.")
    run_cmd.add_argument("file", type=Path, help="CADL file with enforced contracts")
    run_cmd.add_argument("--trace", type=Path, required=True,
                         help="Recorded messages (JSON Lines, the trace format of "
                              "norms-eval with 'epoch' and 'instances')")
    run_cmd.add_argument("--format", choices=["text", "json"], default="text",
                         help="Output format (default: text)")
    run_cmd.add_argument("--audit", type=Path, default=None, metavar="OUT.jsonl",
                         help="Write the audit records (G.8) as JSON Lines, with a "
                              "header, an end record and a hash chain")
    run_cmd.add_argument("--audit-key", type=Path, default=None, metavar="FILE",
                         help="Key the hash chain of the audit log (HMAC-SHA256) with "
                              "the key in FILE (at least 16 bytes)")

    # audit-verify command
    averify_cmd = subparsers.add_parser(
        "audit-verify",
        help="Verify the hash chain of an audit log written by agents-run or "
             "mcp-gateway or a2a-gateway (agents 0.1, G.8)",
        description="Verify the integrity fields of an audit log (G.8, integrity "
                    "of the log): the header, the sequence numbers, the digest of "
                    "every line and the link to the previous line, and the end "
                    "record. Reports the first line that does not verify. Exit "
                    "status: 0 valid, 1 invalid, 2 usage error.")
    averify_cmd.add_argument("audit", type=Path, metavar="AUDIT.jsonl",
                             help="Audit log (JSON Lines)")
    averify_cmd.add_argument("--key", type=Path, default=None, metavar="FILE",
                             help="Key file of a keyed log (HMAC-SHA256)")
    averify_cmd.add_argument("--source", type=Path, default=None, metavar="FILE.cadl",
                             help="Also require that the log was written for this "
                                  "CADL file (SHA-256 of its bytes)")
    averify_cmd.add_argument("--open", action="store_true",
                             help="Accept a log without end record (a running "
                                  "gateway); truncation of its tail is then not "
                                  "detected")
    averify_cmd.add_argument("--run-id", default=None, metavar="HEX",
                             help="Also require this run id in the header")
    averify_cmd.add_argument("--anchor", default=None, metavar="DIGEST",
                             help="Also require this digest of the last line, kept "
                                  "outside the log (detects a rewritten or cut log "
                                  "without a key)")
    averify_cmd.add_argument("--format", choices=["text", "json"], default="text",
                             help="Output format (default: text)")

    # mcp-gateway command
    gw_cmd = subparsers.add_parser(
        "mcp-gateway",
        usage="cadl mcp-gateway FILE --agent ACTOR --server-actor TOOLSERVER "
              "--audit OUT.jsonl [options] -- SERVER_COMMAND [ARGS...]",
        help="Run an MCP stdio gateway in front of a tool server that decides "
             "every tools/call with the enforcement point (agents 0.1)",
        description="A stdio proxy between an MCP client (the agent ACTOR) and an "
                    "MCP server (the tool server TOOLSERVER of the file), started "
                    "as SERVER_COMMAND. Every message is forwarded unchanged, "
                    "except tools/call, which is decided by the online runtime "
                    "of agents-run: allowed calls are forwarded, denied calls "
                    "are answered with a tool result with isError, escalated "
                    "calls are held until an approval, a rejection or the "
                    "timeout (see mcp-approve). Writes the audit log and the "
                    "trace that agents-run replays to the same decisions. Exit "
                    "status: 0 after the client closed its input, 1 when the "
                    "tool server or the enforcement point failed, 2 usage error.")
    gw_cmd.add_argument("file", type=Path, help="CADL file with enforced contracts")
    gw_cmd.add_argument("--agent", required=True, metavar="ACTOR",
                        help="The actor that the MCP client is (the sender of tool calls)")
    gw_cmd.add_argument("--server-actor", required=True, metavar="TOOLSERVER",
                        help="The tool server of the file that the MCP server is "
                             "(agent: { kind: tool_server, protocol: mcp })")
    gw_cmd.add_argument("--audit", type=Path, required=True, metavar="OUT.jsonl",
                        help="Audit log to create (must not exist)")
    gw_cmd.add_argument("--audit-key", type=Path, default=None, metavar="FILE",
                        help="Key the hash chain of the audit log (HMAC-SHA256)")
    gw_cmd.add_argument("--trace-out", type=Path, default=None, metavar="TRACE.jsonl",
                        help="Trace to create (must not exist; default: the audit "
                             "path with the suffix .trace.jsonl)")
    gw_cmd.add_argument("--approvals", type=Path, default=None, metavar="DIR",
                        help="Inbox directory for approvals, rejections and other "
                             "messages (mcp-approve, mcp-message); a directory of this "
                             "user that nobody else can write. Without it no hold can "
                             "be approved")
    gw_cmd.add_argument("--approver-keys", type=Path, default=None, metavar="FILE",
                        help="JSON object from actor to the key file (or {\"hex\": ...}) "
                             "with which its inbox files are authenticated "
                             "(HMAC-SHA256); required with --approvals")
    gw_cmd.add_argument("--insecure-approvals", action="store_true",
                        help="Read the inbox without authentication (for tests only)")
    gw_cmd.add_argument("--allow-method", action="append", default=[], metavar="NAME",
                        help="Also pass this method (repeatable); methods other than "
                             "tools/call are not decided by the norms")
    gw_cmd.add_argument("--init", type=Path, default=None, metavar="FILE.json",
                        help="Keys for the first line of the trace: bindings, "
                             "request, values, quantities, instances, "
                             "host_instances, state")
    gw_cmd.add_argument("--no-filter-tools", action="store_true",
                        help="Do not remove undeclared tools from tools/list results")
    gw_cmd.add_argument("--poll", type=float, default=0.05, metavar="SECONDS",
                        help="Interval for the inbox and for deadlines (default 0.05)")

    # a2a-gateway command
    a2a_cmd = subparsers.add_parser(
        "a2a-gateway",
        usage="cadl a2a-gateway FILE --agent-actor ACTOR --upstream URL "
              "--client-keys KEYS.json --audit OUT.jsonl [options]",
        help="Run an A2A gateway (JSON-RPC over HTTP) in front of an A2A agent that "
             "decides every SendMessage with the enforcement point (agents 0.1)",
        description="An HTTP server in front of the A2A agent ACTOR of the file, "
                    "reached at URL (A2A 1.0.0, JSON-RPC binding). Every request is "
                    "authenticated with the key of its actor (header Authorization: "
                    "CADL-HMAC). A SendMessage names its CADL message with the "
                    "extension of G.11 and is decided by the online runtime of "
                    "agents-run: allowed messages are passed on, denied messages "
                    "are answered with a task in TASK_STATE_REJECTED, escalated "
                    "messages are held until an approval, a rejection or the "
                    "timeout (see mcp-approve). GetTask and CancelTask are passed "
                    "on for the client's own tasks; other methods are refused. "
                    "Writes the audit log and the trace that agents-run replays "
                    "to the same decisions. Exit status: 0 after SIGTERM or "
                    "SIGINT, 1 when the enforcement point failed, 2 usage error.")
    a2a_cmd.add_argument("file", type=Path, help="CADL file with enforced contracts")
    a2a_cmd.add_argument("--agent-actor", required=True, metavar="ACTOR",
                         help="The actor that the A2A agent behind the gateway is "
                              "(agent: { kind: llm_agent, protocol: a2a }); the receiver "
                              "of every message")
    a2a_cmd.add_argument("--upstream", required=True, metavar="URL",
                         help="The JSON-RPC endpoint of the A2A agent (http or https)")
    a2a_cmd.add_argument("--client-keys", type=Path, required=True, metavar="FILE",
                         help="JSON object from actor to the key file (or {\"hex\": ...}) "
                              "with which its requests are authenticated (HMAC-SHA256)")
    a2a_cmd.add_argument("--audit", type=Path, required=True, metavar="OUT.jsonl",
                         help="Audit log to create (must not exist)")
    a2a_cmd.add_argument("--audit-key", type=Path, default=None, metavar="FILE",
                         help="Key the hash chain of the audit log (HMAC-SHA256)")
    a2a_cmd.add_argument("--trace-out", type=Path, default=None, metavar="TRACE.jsonl",
                         help="Trace to create (must not exist; default: the audit "
                              "path with the suffix .trace.jsonl)")
    a2a_cmd.add_argument("--approvals", type=Path, default=None, metavar="DIR",
                         help="Inbox directory for approvals, rejections and other "
                              "messages (mcp-approve, mcp-message)")
    a2a_cmd.add_argument("--approver-keys", type=Path, default=None, metavar="FILE",
                         help="The keys of the approvers and other senders of the "
                              "inbox; required with --approvals")
    a2a_cmd.add_argument("--insecure-approvals", action="store_true",
                         help="Read the inbox without authentication (for tests only)")
    a2a_cmd.add_argument("--init", type=Path, default=None, metavar="FILE.json",
                         help="Keys for the first line of the trace: bindings, "
                              "request, values, quantities, instances, "
                              "host_instances, state")
    a2a_cmd.add_argument("--listen", default="127.0.0.1:8470", metavar="HOST:PORT",
                         help="Address to listen on (default 127.0.0.1:8470; port 0 "
                              "picks a free port). An address other than loopback "
                              "needs --tls-cert and --tls-key")
    a2a_cmd.add_argument("--port-file", type=Path, default=None, metavar="FILE",
                         help="Write the port that the gateway listens on to FILE")
    a2a_cmd.add_argument("--tls-cert", type=Path, default=None, metavar="FILE",
                         help="Certificate chain (PEM) for HTTPS")
    a2a_cmd.add_argument("--tls-key", type=Path, default=None, metavar="FILE",
                         help="Private key (PEM) for HTTPS")
    a2a_cmd.add_argument("--agent-card", type=Path, default=None, metavar="FILE.json",
                         help="Agent card served at /.well-known/agent-card.json")
    a2a_cmd.add_argument("--upstream-token", type=Path, default=None, metavar="FILE",
                         help="Bearer token for the agent, read from FILE")
    a2a_cmd.add_argument("--upstream-timeout", type=float, default=300.0,
                         metavar="SECONDS", help="Timeout of a request to the agent "
                                                 "(default 300)")
    a2a_cmd.add_argument("--max-handlers", type=int, default=64, metavar="N",
                         help="Authenticated requests handled at the same time, held "
                              "blocking requests included (default 64); beyond that 503")
    a2a_cmd.add_argument("--max-readers", type=int, default=64, metavar="N",
                         help="Connections whose request is being read at the same time, "
                              "before authentication (default 64); beyond that 503")
    a2a_cmd.add_argument("--max-readers-per-address", type=int, default=4, metavar="N",
                         help="Of those, connections of one remote address, IPv4 address or IPv6 /64 (default 4); "
                              "beyond that the connection is closed at once")
    a2a_cmd.add_argument("--max-handlers-per-actor", type=int, default=16, metavar="N",
                         help="Authenticated requests of one actor handled at the same "
                              "time, held blocking requests included (default 16); "
                              "beyond that 503")
    a2a_cmd.add_argument("--max-holds-per-actor", type=int, default=64, metavar="N",
                         help="Holds of one actor, blocking or not (default 64); a further "
                              "SendMessage of that actor gets 503 before its decision")
    a2a_cmd.add_argument("--read-timeout", type=float, default=10.0, metavar="SECONDS",
                         help="Time to read the request line, the headers and the body "
                              "of a request (default 10)")
    a2a_cmd.add_argument("--poll", type=float, default=0.05, metavar="SECONDS",
                         help="Interval for the inbox and for deadlines (default 0.05)")

    # mcp-approve command
    approve_cmd = subparsers.add_parser(
        "mcp-approve",
        help="Approve or reject a hold of a running mcp-gateway (agents 0.1, G.6)",
        description="Drop an approval or a rejection into the inbox directory of "
                    "mcp-gateway. The gateway turns it into the message of the "
                    "approver that the file's counts_as rule maps to the event "
                    "approval (rejection), with the hold id as its first "
                    "argument, and decides that message like any other. Exit "
                    "status: 0 written, 2 usage error.")
    approve_cmd.add_argument("inbox", type=Path, metavar="DIR", help="Inbox directory")
    approve_cmd.add_argument("hold_id", metavar="HOLD_ID", help="Hold id, e.g. h-1")
    approve_cmd.add_argument("decision", choices=["approve", "reject"])
    approve_cmd.add_argument("--as", dest="actor", required=True, metavar="ACTOR",
                             help="The actor who approves or rejects")
    approve_cmd.add_argument("--show", action="store_true",
                             help="Show the held call and its arguments, and write "
                                  "nothing")
    approve_cmd.add_argument("--key", type=Path, default=None, metavar="FILE",
                             help="The key of the actor (HMAC-SHA256); the gateway "
                                  "requires it unless it runs with --insecure-approvals")

    # mcp-message command
    msg_cmd = subparsers.add_parser(
        "mcp-message",
        help="Report another governed message to a running mcp-gateway, such as a "
             "grant between agents (agents 0.1)",
        description="Drop a message 'FROM -> TO : name' into the inbox directory of "
                    "mcp-gateway. The gateway decides it at its arrival like any "
                    "message of a trace. Exit status: 0 written, 2 usage error.")
    msg_cmd.add_argument("inbox", type=Path, metavar="DIR", help="Inbox directory")
    msg_cmd.add_argument("message", metavar="'FROM -> TO : name'",
                         help="The message, e.g. 'OPERATOR -> PLANNER : grant_read'")
    msg_cmd.add_argument("--arg", action="append", default=[], metavar="VALUE",
                         help="An argument of the message (repeatable; strings)")
    msg_cmd.add_argument("--key", type=Path, default=None, metavar="FILE",
                         help="The key of the sender (HMAC-SHA256)")

    # regime-map command
    regime_cmd = subparsers.add_parser("regime-map", help="Analyze regime transitions and build a regime map")
    regime_cmd.add_argument("file", type=Path, help="CADL file to analyze")
    regime_cmd.add_argument("--format", choices=["text", "dot", "json"], default="text",
                             help="Output format (default: text)")
    regime_cmd.add_argument("--output", "-o", type=Path, default=None,
                             help="Save output to file")

    # iec62853 command
    iec_cmd = subparsers.add_parser("iec62853", help="Generate an IEC 62853-oriented dependability summary (informative; not a conformance assessment)")
    iec_cmd.add_argument("file", type=Path, help="CADL file to analyze")
    iec_cmd.add_argument("--format", choices=["text", "json"], default="text",
                         help="Output format (default: text)")
    iec_cmd.add_argument("--output", "-o", type=Path, default=None,
                         help="Save output to file")

    # sim-validate command
    sim_val_cmd = subparsers.add_parser("sim-validate",
        help="Parse CADL, lower to simulator IR, and validate")
    sim_val_cmd.add_argument("file", type=Path, help="CADL file to validate")

    # sim-ir command
    sim_ir_cmd = subparsers.add_parser("sim-ir",
        help="Print the 3-layer simulator IR for a CADL file")
    sim_ir_cmd.add_argument("file", type=Path, help="CADL file to lower")
    sim_ir_cmd.add_argument("--format", choices=["yaml", "json"], default="yaml",
                            help="Output format (default: yaml)")

    # sim-gen command
    sim_gen_cmd = subparsers.add_parser("sim-gen",
        help="Generate simulator config from a CADL file")
    sim_gen_cmd.add_argument("file", type=Path, help="CADL file to generate from")
    sim_gen_cmd.add_argument("--target", "-t", choices=["python", "unity", "go"],
                             required=True, help="Simulator target")
    sim_gen_cmd.add_argument("--output", "-o", type=Path, default=None,
                             help="Output file (default: stdout)")

    # norms-eval command
    norms_cmd = subparsers.add_parser("norms-eval",
        help="Compute the verdicts of a norms 0.1 contract on a recorded trace (Appendix F, F.5)")
    norms_cmd.add_argument("file", type=Path, help="CADL file that declares norms: 0.1")
    norms_cmd.add_argument("--trace", type=Path, required=True,
                           help="Timed trace of one contract instance (JSON Lines)")
    norms_cmd.add_argument("--contract", default=None, metavar="ID",
                           help="Contract to evaluate (default: the only one with norms)")
    norms_cmd.add_argument("--format", choices=["text", "json"], default="text",
                           help="Output format (default: text)")

    # norms-check command
    nchk_cmd = subparsers.add_parser("norms-check",
        help="Check the norms of one contract instance over all behaviours with a model "
             "checker (Appendix F, F.12)")
    nchk_cmd.add_argument("file", type=Path, help="CADL file that declares norms: 0.1")
    nchk_cmd.add_argument("--contract", default=None, metavar="ID",
                          help="Contract to check (default: the only one with norms)")
    nchk_cmd.add_argument("--bindings", action="append", default=[], metavar="i=n",
                          help="Bind an index of the instance (default: the lower bound of "
                               "its set); may be repeated")
    nchk_cmd.add_argument("--members", action="append", default=[], metavar="SET=n",
                          help="Number of members of a set of actors in the instance "
                               "(default: as declared, else up to the largest bound index)")
    nchk_cmd.add_argument("--step", default=None, metavar="DURATION",
                          help="Time between two observations, e.g. 1s or 500ms (default: the "
                               "greatest common divisor of the durations of the contract)")
    nchk_cmd.add_argument("--horizon", type=int, default=None, metavar="N",
                          help="Last observation of an instance (default: from the deadlines "
                               "and intervals of the norms)")
    nchk_cmd.add_argument("--export", type=Path, default=None, metavar="DIR",
                          help="Write the PRISM model, the properties and the witness traces "
                               "to DIR")
    nchk_cmd.add_argument("--timeout", type=float, default=600, metavar="SECONDS",
                          help="Time for all runs of the model checker together; exit status 2 "
                               "when it is exceeded (default: 600)")
    nchk_cmd.add_argument("--require-checker", dest="require_checker", action="store_true",
                          help="Exit with status 2 when PRISM is not on PATH (default: report "
                               "that nothing was checked and exit with 0)")
    nchk_cmd.add_argument("--format", choices=["text", "json"], default="text",
                          help="Output format (default: text)")

    # norms-diff command
    diff_cmd = subparsers.add_parser("norms-diff",
        help="Report what an amendment from OLD to NEW does to the norms (Appendix F, F.6)")
    diff_cmd.add_argument("old", type=Path, help="Old version of the CADL file")
    diff_cmd.add_argument("new", type=Path, help="New version of the CADL file")
    diff_cmd.add_argument("--contract", default=None, metavar="ID",
                          help="Contract to compare (default: every contract with norms)")
    diff_cmd.add_argument("--format", choices=["text", "json"], default="text",
                          help="Output format (default: text)")

    # norms-indices command (grounding extension, Appendix J)
    nidx_cmd = subparsers.add_parser("norms-indices",
        help="Report the derived indices of information sharing and concentration of "
             "powers and the incentive coverage of each contract (grounding 0.1, "
             "Appendix J, J.4)")
    nidx_cmd.add_argument("file", type=Path,
                          help="CADL file that declares norms: 0.1 and grounding: 0.1")
    nidx_cmd.add_argument("--contract", default=None, metavar="ID",
                          help="Contract to report (default: every contract)")
    nidx_cmd.add_argument("--format", choices=["text", "json"], default="text",
                          help="Output format (default: text)")

    # model-export command (stochastic extension, Appendix H)
    mexp_cmd = subparsers.add_parser("model-export",
        help="Export the model: block (stochastic 0.1) to PRISM, one model file "
             "and one properties file per design")
    mexp_cmd.add_argument("file", type=Path, help="CADL file to export from")
    mexp_cmd.add_argument("--output", "-o", type=Path, default=Path("."),
                          help="Output directory (default: current directory)")
    mexp_cmd.add_argument("--regime", default=None,
                          help="Export only the design of this regime")

    # model-check command (stochastic extension, Appendix H)
    mchk_cmd = subparsers.add_parser("model-check",
        help="Evaluate the properties of the model: block (stochastic 0.1) with the "
             "internal reference evaluator (exact arithmetic; not a model checker), "
             "or compute a certified regime map (--regime-map ID --certify)")
    mchk_cmd.add_argument("file", type=Path, help="CADL file to evaluate")
    mchk_cmd.add_argument("--regime", default=None,
                          help="Evaluate only the design of this regime")
    mchk_cmd.add_argument("--set", dest="assignments", action="append", default=[],
                          metavar="p=v", help="Give parameter p the value v (a decimal "
                          "or n/d) instead of its fixed value; may be repeated")
    mchk_cmd.add_argument("--regime-map", dest="regime_map", default=None, metavar="ID",
                          help="Evaluate this regime map query: at sample points "
                               "(estimates; the default) or, with --certify, as a "
                               "certified regime map")
    mchk_cmd.add_argument("--grid", type=int, default=None, metavar="N",
                          help="With --regime-map: intervals per open parameter of the "
                               "uniform grid whose corner points are evaluated (default: 4)")
    mchk_cmd.add_argument("--certify", action="store_true",
                          help="With --regime-map: compute the certified regime map "
                               "(cells decided by parameter lifting with exact arithmetic)")
    mchk_cmd.add_argument("--max-depth", dest="max_depth", type=int, default=None, metavar="N",
                          help="With --certify: bisect a cell at most N times (default: 24)")
    mchk_cmd.add_argument("--max-cells", dest="max_cells", type=int, default=None, metavar="N",
                          help="With --certify: refine the partition to at most N cells "
                               "(default: 20000)")
    mchk_cmd.add_argument("--format", choices=["text", "json"], default="text",
                          help="Output format (default: text)")

    # game-check command (strategic model extension, Appendix I)
    gchk_cmd = subparsers.add_parser("game-check",
        help="Evaluate the equilibrium checks of a strategic model (games 0.1) with "
             "exact arithmetic and, with --certify, compute its equilibrium maps")
    gchk_cmd.add_argument("file", type=Path, help="CADL file to evaluate")
    gchk_cmd.add_argument("--equilibrium", dest="equilibria", action="append", default=[],
                          metavar="ID", help="Evaluate only this entry of "
                          "model.equilibria; may be repeated (default: all)")
    gchk_cmd.add_argument("--regime", default=None,
                          help="Evaluate only the design of this regime")
    gchk_cmd.add_argument("--set", dest="assignments", action="append", default=[],
                          metavar="p=v", help="Give parameter p the value v (a decimal "
                          "or n/d) instead of its fixed value; may be repeated")
    gchk_cmd.add_argument("--certify", action="store_true",
                          help="Also compute the equilibrium maps (entries with over:), "
                               "with certified cells")
    gchk_cmd.add_argument("--max-depth", dest="max_depth", type=int, default=None, metavar="N",
                          help="With --certify: bisect a cell at most N times (default: 24)")
    gchk_cmd.add_argument("--max-cells", dest="max_cells", type=int, default=None, metavar="N",
                          help="With --certify: refine the partition to at most N cells "
                               "(default: 20000)")
    gchk_cmd.add_argument("--partition", type=int, default=None, metavar="N",
                          help="With --certify: start from the uniform partition of the "
                               "region into N intervals per open parameter")
    gchk_cmd.add_argument("--no-merge", dest="no_merge", action="store_true",
                          help="With --certify: report the cells as analysed, without "
                               "joining cells that carry the same claim")
    gchk_cmd.add_argument("--format", choices=["text", "json"], default="text",
                          help="Output format (default: text)")

    # ai command
    ai_cmd = subparsers.add_parser("ai", help="Generate CADL from natural language description")
    ai_cmd.add_argument("description", nargs="?", default=None,
                        help="Natural language description of the SoS")
    ai_cmd.add_argument("-f", "--file", type=Path, dest="input_file",
                        help="Read description from a file")
    ai_cmd.add_argument("-o", "--output", type=Path, dest="output_file",
                        help="Save generated CADL to file")
    ai_cmd.add_argument("--verify", action="store_true",
                        help="Run SMT verification and deadlock detection on generated CADL")
    ai_cmd.add_argument("--model", type=str, default=None,
                        help="Override LLM model (default: $CADL_LLM_MODEL, else claude-sonnet-5-5)")

    # `cadl mcp-gateway ... -- SERVER_COMMAND [ARGS...]`: everything after
    # the first `--` is the command of the MCP server.
    argv = list(sys.argv[1:] if argv is None else argv)
    server_cmd = None
    if argv[:1] == ["mcp-gateway"] and "--" in argv:
        cut = argv.index("--")
        argv, server_cmd = argv[:cut], argv[cut + 1:]
    args = parser.parse_args(argv)
    if args.command == "mcp-gateway":
        args.server = server_cmd or []

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "parse":
        return _cmd_parse(args)
    elif args.command == "check":
        return _cmd_check(args)
    elif args.command == "verify":
        return _cmd_verify(args)
    elif args.command == "codegen":
        return _cmd_codegen(args)
    elif args.command == "agents-decide":
        return _cmd_agents_decide(args)
    elif args.command == "regime-map":
        return _cmd_regime_map(args)
    elif args.command == "iec62853":
        return _cmd_iec62853(args)
    elif args.command == "sim-validate":
        return _cmd_sim_validate(args)
    elif args.command == "sim-ir":
        return _cmd_sim_ir(args)
    elif args.command == "sim-gen":
        return _cmd_sim_gen(args)
    elif args.command == "agents-run":
        return _cmd_agents_run(args)
    elif args.command == "audit-verify":
        return _cmd_audit_verify(args)
    elif args.command == "mcp-gateway":
        from .mcp_gateway import cli_gateway
        return cli_gateway(args)
    elif args.command == "a2a-gateway":
        from .a2a_gateway import cli_gateway as a2a_cli_gateway
        return a2a_cli_gateway(args)
    elif args.command == "mcp-approve":
        from .mcp_gateway import cli_approve
        return cli_approve(args)
    elif args.command == "mcp-message":
        from .mcp_gateway import cli_message
        return cli_message(args)
    elif args.command == "norms-eval":
        return _cmd_norms_eval(args)
    elif args.command == "norms-check":
        return _cmd_norms_check(args)
    elif args.command == "norms-diff":
        return _cmd_norms_diff(args)
    elif args.command == "norms-indices":
        return _cmd_norms_indices(args)
    elif args.command == "model-export":
        return _cmd_model_export(args)
    elif args.command == "model-check":
        return _cmd_model_check(args)
    elif args.command == "game-check":
        return _cmd_game_check(args)
    elif args.command == "ai":
        return _cmd_ai(args)

    return 0


def _cmd_parse(args: argparse.Namespace) -> int:
    from .parser import parse_file

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    print(f"Successfully parsed: {path}")
    print(f"  SoS name: {sos.name}")
    if sos.type:
        print(f"  Type: {sos.type.value}")
    if sos.version:
        print(f"  Version: {sos.version}")
    print(f"  Actors: {len(sos.actors)}")
    print(f"  Contracts: {len(sos.contracts)}")
    print(f"  Protocols: {len(sos.protocols)}")
    print(f"  Metrics: {len(sos.metrics)}")

    if args.ast:
        import pprint
        pprint.pprint(sos)

    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .type_checker import type_check

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    result = type_check(sos)

    for info in result.infos:
        print(f"  {info}", file=sys.stderr)
    for warning in result.warnings:
        print(f"  {warning}", file=sys.stderr)
    for error in result.errors:
        print(f"  {error}", file=sys.stderr)

    if result.ok:
        print(f"Type check passed: {path}")
        if result.warnings:
            print(f"  ({len(result.warnings)} warning(s))")
        return 0
    else:
        print(f"Type check failed: {len(result.errors)} error(s), {len(result.warnings)} warning(s)")
        return 1


def _cmd_verify(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .type_checker import type_check
    from .verifier import verify
    from .deadlock import detect_deadlocks

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    # Parse
    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    # Run checks
    tc_result = type_check(sos)
    v_results = verify(sos)
    d_results = detect_deadlocks(sos)

    has_failures = not tc_result.ok or \
        any(r.status == "failed" for r in v_results) or \
        any(r.status == "failed" for r in d_results)

    # "info" and "not_supported" results are findings, not checks with a
    # verdict, so they are left out of the pass/fail totals.
    judged = ("passed", "failed", "unknown", "warning")
    total_checks = sum(1 for r in v_results if r.status in judged) + \
                   sum(1 for r in d_results if r.status in judged) + \
                   1  # +1 for type check
    passed = sum(1 for r in v_results if r.status == "passed") + \
             sum(1 for r in d_results if r.status == "passed") + \
             (1 if tc_result.ok else 0)
    failed = sum(1 for r in v_results if r.status == "failed") + \
             sum(1 for r in d_results if r.status == "failed") + \
             (0 if tc_result.ok else 1)

    # JSON output
    if args.format == "json":
        import json
        output = {
            "file": str(path),
            "sos": sos.name,
            "type_check": {
                "ok": tc_result.ok,
                "errors": [str(e) for e in tc_result.errors],
                "warnings": [str(w) for w in tc_result.warnings],
                "infos": [str(i) for i in tc_result.infos],
            },
            "verification": [
                {"name": r.check_name, "status": r.status, "message": r.message,
                 "counterexample": r.counterexample}
                for r in v_results
            ],
            "deadlock": [
                {"name": r.check_name, "status": r.status, "message": r.message,
                 "details": getattr(r, 'details', None)}
                for r in d_results
            ],
            "summary": {
                "total": total_checks,
                "passed": passed,
                "failed": failed,
            }
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 1 if has_failures else 0

    # Text output
    print(f"Verifying: {path}")
    print(f"  SoS: {sos.name}")
    print()

    print("--- Type Check ---")
    for i in tc_result.infos:
        print(f"  {i}")
    for w in tc_result.warnings:
        print(f"  {w}")
    for e in tc_result.errors:
        print(f"  {e}")
    if tc_result.ok:
        print("  [PASS] Type check passed")
    else:
        print(f"  [FAIL] {len(tc_result.errors)} error(s)")
    print()

    print("--- SMT Verification ---")
    for r in v_results:
        print(f"  {r}")
    if not v_results:
        print("  (no contracts to verify)")
    print()

    print("--- Deadlock Detection ---")
    for r in d_results:
        print(f"  {r}")
    if not d_results:
        print("  (no protocols to analyze)")
    print()

    if has_failures:
        print(f"Verification FAILED: {passed}/{total_checks} checks passed")
        return 1
    else:
        print(f"Verification PASSED: {passed}/{total_checks} checks passed")
        return 0


def _cmd_codegen(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .type_checker import type_check
    from .codegen import generate, omitted_temporal_items

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    # Parse
    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    # Type check
    tc_result = type_check(sos)
    for w in tc_result.warnings:
        print(f"  {w}", file=sys.stderr)
    if not tc_result.ok:
        for e in tc_result.errors:
            print(f"  {e}", file=sys.stderr)
        print(f"Type check failed: cannot generate code", file=sys.stderr)
        return 1

    # Generate
    target = args.target
    output_dir = args.output
    from .codegen import norms_monitors
    try:
        monitors = {m["contract"]: m for m in norms_monitors(sos, target)}
    except Exception as e:
        print(f"Code generation error: {e}", file=sys.stderr)
        return 1
    for contract_id, key, text in omitted_temporal_items(sos):
        if target == "python":
            print(f"  [WARNING]: {key} item of contract '{contract_id}' has a temporal "
                  f"operator (norms 0.1) and is left out of contracts.py; "
                  f"norms_monitor.py monitors it: {text}", file=sys.stderr)
        elif contract_id in monitors and monitors[contract_id]["text"] is not None \
                and not any(k == key and t == text for k, _, t, _ in monitors[contract_id]["left_out"]):
            print(f"  [WARNING]: {key} item of contract '{contract_id}' has a temporal "
                  f"operator (norms 0.1) and is left out of the contract policy; "
                  f"{monitors[contract_id]['file']} monitors it: {text}", file=sys.stderr)
        else:
            print(f"  [WARNING]: {key} item of contract '{contract_id}' has a temporal "
                  f"operator (norms 0.1) and is not generated (no norms monitor of the "
                  f"{target} target monitors it; the python target does): {text}",
                  file=sys.stderr)
    for m in monitors.values():
        if m["text"] is None:
            print(f"  [WARNING]: contract '{m['contract']}': no norms monitor is generated "
                  f"for the {target} target, so its norms, counts-as rules and items are "
                  f"not monitored ({'; '.join(m['reasons'])}); the python target "
                  f"monitors them (norms_monitor.py)", file=sys.stderr)
        for norm in m.get("gas", []) if m["text"] is not None else []:
            print(f"  [WARNING]: contract '{m['contract']}', norm '{norm}': its content has a "
                  f"temporal operator; in {m['file']} each observation inside an open window "
                  f"of it costs about 4 x 10^4 gas on every later observation until the window "
                  f"closes; keep such observations well below the block gas limit (a monitor "
                  f"that reaches it cannot record any further observation)", file=sys.stderr)
        for key, pos, text, reason in m["left_out"]:
            print(f"  [WARNING]: {key} item {pos} of contract '{m['contract']}' is not "
                  f"monitored by {m['file']} ({reason}): {text}", file=sys.stderr)
    try:
        generate(sos, output_dir, target=target)
    except Exception as e:
        print(f"Code generation error: {e}", file=sys.stderr)
        return 1

    print(f"Code generated ({target}): {output_dir}/")
    print(f"  SoS: {sos.name}")
    print(f"  Actors: {len(sos.actors)}")
    print(f"  Contracts: {len(sos.contracts)}")
    print(f"  Protocols: {len(sos.protocols)}")
    if target == "python":
        print(f"  Metrics: {len(sos.metrics)}")
        from .codegen.norms_monitor_gen import has_norms_monitor
        if has_norms_monitor(sos):
            print(f"  Norms monitor: norms_monitor.py "
                  f"({sum(len(c.norms) for c in sos.contracts)} norm(s), "
                  f"{sum(len(c.counts_as) for c in sos.contracts)} counts-as rule(s))")
    for m in monitors.values():
        if m["text"] is not None:
            c = next(c for c in sos.contracts if c.id == m["contract"])
            print(f"  Norms monitor: {m['file']} ({len(c.norms)} norm(s), "
                  f"{len(c.counts_as)} counts-as rule(s))")
    return 0


# Exit status of `cadl agents-decide`, by decision; 2 is a usage error.
_DECISION_STATUS = {"allow": 0, "deny": 1, "escalate": 3}


def _cmd_agents_decide(args: argparse.Namespace) -> int:
    import json
    from .agents import AgentsInputError, build_policy, decide, format_result
    from .parser import parse_file
    from .type_checker import type_check

    def usage_error(message: str) -> int:
        print(f"Error: {message}", file=sys.stderr)
        return 2

    path = args.file
    if not path.exists():
        return usage_error(f"File not found: {path}")
    try:
        sos = parse_file(path)
    except Exception as e:
        return usage_error(f"Parse error: {e}")
    tc_result = type_check(sos)
    if not tc_result.ok:
        for e in tc_result.errors:
            print(f"  {e}", file=sys.stderr)
        return usage_error("Type check failed: cannot decide")
    try:
        policy = build_policy(sos)
    except ValueError as e:
        return usage_error(str(e))

    try:
        if str(args.input) == "-":
            text = sys.stdin.read()
        else:
            text = args.input.read_text(encoding="utf-8")
        result = decide(policy, json.loads(text))
    except OSError as e:
        return usage_error(f"Cannot read the policy input: {e}")
    except json.JSONDecodeError as e:
        return usage_error(f"The policy input is not JSON: {e}")
    except AgentsInputError as e:
        return usage_error(f"Invalid policy input: {e}")

    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_result(result))
    return _DECISION_STATUS[result["decision"]]


def _cmd_regime_map(args: argparse.Namespace) -> int:
    import json as json_mod
    from .parser import parse_file
    from .regime_map import RegimeMap

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    rm = RegimeMap.from_sos(sos)

    if not rm.states:
        print("No transitions defined in this CADL file.", file=sys.stderr)
        return 0

    if args.format == "dot":
        output = rm.to_dot()
    elif args.format == "json":
        output = json_mod.dumps(rm.to_json(), indent=2, ensure_ascii=False)
    else:
        output = rm.to_text()

    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"Saved to: {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


def _cmd_iec62853(args: argparse.Namespace) -> int:
    import json as json_mod
    from .parser import parse_file
    from .iec62853 import generate_iec62853_report

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    report = generate_iec62853_report(sos)

    if args.format == "json":
        output = json_mod.dumps(report, indent=2, ensure_ascii=False)
    else:
        output = _format_iec62853_text(report)

    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"Saved to: {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


def _format_iec62853_text(report: dict) -> str:
    lines = []
    lines.append(f"Dependability Summary (IEC 62853-oriented): {report['sos_name']}")
    lines.append("=" * 60)
    if report.get("disclaimer"):
        lines.append(f"Note: {report['disclaimer']}")
    lines.append("")

    lines.append(f"Integration Level (CADL-defined): {report['system_integration_level']}")
    lines.append(f"SoS Type: {report['sos_type']}")
    lines.append("")

    lines.append("--- Institutional Parameters ---")
    for param in report.get("institutional_parameters", []):
        lines.append(f"  {param['cadl_concept']}: {param['value']}")
        lines.append(f"    Indicator (CADL-defined): {param['iec62853_concept']}")
    lines.append("")

    lines.append("--- Service Level Agreements ---")
    for sla in report.get("service_level_agreements", []):
        lines.append(f"  {sla['contract_id']}:")
        lines.append(f"    Assumptions: {sla['assumption_count']}")
        lines.append(f"    Guarantees: {sla['guarantee_count']}")
        if sla.get("failure_response"):
            lines.append(f"    Failure Response: {sla['failure_response']}")
    lines.append("")

    if report.get("operational_state_machine"):
        lines.append("--- Operational State Machine ---")
        osm = report["operational_state_machine"]
        lines.append(f"  States: {osm['state_count']}")
        lines.append(f"  Transitions: {osm['transition_count']}")
        if osm.get("initial_state"):
            lines.append(f"  Initial State: {osm['initial_state']}")
        lines.append("")

    lines.append("--- Dependability Summary ---")
    dep = report.get("dependability_summary", {})
    lines.append(f"  Total Contracts: {dep.get('total_contracts', 0)}")
    lines.append(f"  Total Actors: {dep.get('total_actors', 0)}")
    lines.append(f"  Governance Index (avg beta): {dep.get('governance_index', 'N/A')}")
    lines.append(f"  Transparency Level (avg alpha): {dep.get('transparency_level', 'N/A')}")
    lines.append(f"  Alignment Metric (avg lambda): {dep.get('alignment_metric', 'N/A')}")

    return "\n".join(lines)


def _cmd_sim_validate(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .sim import lower_to_ir, validate_ir

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    ir = lower_to_ir(sos)
    errors = validate_ir(ir)

    if errors:
        print(f"Validation FAILED for {path}:")
        for err in errors:
            print(f"  - {err}")
        return 1
    else:
        print(f"Validation OK: {path}")
        print(f"  SoS: {ir.name} ({ir.sos_type})")
        print(f"  Actors: {len(ir.institution.actors)}")
        print(f"  Contracts: {len(ir.institution.contracts)}")
        print(f"  Protocols: {len(ir.protocol.protocols)}")
        print(f"  Algorithms: {len(ir.algorithm.algorithms)}")
        print(f"  Transitions: {len(ir.transitions)}")
        return 0


def _cmd_sim_ir(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .sim import lower_to_ir
    from .sim.ir import SimIR

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    ir = lower_to_ir(sos)

    if args.format == "json":
        import json
        d = _ir_to_dict(ir)
        if "motivation_block" in d:
            d["motivation_block"] = _jsonable(d["motivation_block"])
        print(json.dumps(d, indent=2, ensure_ascii=False))
    else:
        import yaml
        print(yaml.dump(_ir_to_dict(ir), default_flow_style=False,
                        sort_keys=False, allow_unicode=True))

    return 0


def _ir_to_dict(ir) -> dict:
    """Serialize SimIR to a plain dict for YAML/JSON output."""
    from dataclasses import asdict
    d = asdict(ir)
    # The fields of the agent governance extension (Appendix G, G.13) are
    # part of the IR only for a file that declares it (G.14).
    if not d.pop("agents_extension", False):
        inst = d.get("institution", {})
        for a in inst.get("actors", []):
            for key in ("agent_kind", "agent_protocol", "agent_tools"):
                a.pop(key, None)
        for c in inst.get("contracts", []):
            for key in ("enforced", "enforcement_default", "enforcement_point",
                        "delegations"):
                c.pop(key, None)
            for n in c.get("norms", []):
                for key in ("on_denied_escalate_to", "on_denied_timeout_ms",
                            "on_denied_fallback", "on_denied_notify"):
                    n.pop(key, None)
    # Likewise the fields of the grounding extension (Appendix J, J.10);
    # the quantities of the SoS only when there are any.
    if d.get("institution", {}).get("grounding", 0) is None:
        d["institution"].pop("grounding")
    if not d.pop("grounding_extension", False):
        for c in d.get("institution", {}).get("contracts", []):
            c.pop("grounding", None)
            for n in c.get("norms", []):
                for key in ("shares", "on_violation_magnitude", "on_fulfilment_reward",
                            "on_fulfilment_magnitude"):
                    n.pop(key, None)
    # Rename lambda_ back to lambda for readability
    for c in d.get("institution", {}).get("contracts", []):
        gov = c.get("governance", {})
        if "lambda_" in gov:
            gov["lambda"] = gov.pop("lambda_")
        # Likewise if_ of a counts-as rule (Appendix F, F.9)
        for rule in c.get("counts_as", []):
            if "if_" in rule:
                rule["if"] = rule.pop("if_")
    # The verbatim motivation block is an optional key: present only when
    # the source has a `motivation:` block.
    if d.get("motivation_block") is None:
        d.pop("motivation_block", None)
    return d


def _jsonable(value):
    """Make a YAML value safe for strict JSON (dates, sets, inf, nan → text)."""
    import math
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)


def _cmd_sim_gen(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .sim import lower_to_ir, validate_ir, generate_config

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    ir = lower_to_ir(sos)
    errors = validate_ir(ir)
    if errors:
        print(f"Validation errors:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    config_str = generate_config(ir, args.target)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(config_str, encoding="utf-8")
        print(f"Generated {args.target} config: {args.output}")
    else:
        print(config_str)

    return 0


def _cmd_norms_eval(args: argparse.Namespace) -> int:
    """Exit status: 0 when no norm or guarantee is `violated` (`excused`
    and `pending` do not fail), 1 otherwise, 2 on a usage error."""
    from .norms_eval import NormsEvalError, TraceError, evaluate_files, format_text

    try:
        result = evaluate_files(args.file, args.trace, args.contract)
    except TraceError as e:
        print(f"Trace error: {args.trace}: {e}", file=sys.stderr)
        return 2
    except NormsEvalError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    if args.format == "json":
        import json
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_text(result))
    return 0 if result["ok"] else 1


def _cmd_norms_check(args: argparse.Namespace) -> int:
    """Exit status: 0 when the questions were answered without a finding,
    or when no model checker is on PATH (the model is still exported with
    --export) unless --require-checker is given; 1 on a finding (a
    conflict, a norm never activated or violated in every instance, an
    activation that cannot be kept, a power never exercised); 2 on a usage
    error, a refusal, a missing PRISM with --require-checker, or when the
    model checker exceeds --timeout; 3 when a witness is not confirmed by
    norms-eval (an error of norms-check)."""
    from .norms_check import check_file, find_checker, format_text
    from .norms_eval import NormsEvalError

    if args.require_checker and find_checker() is None:
        print("Error: --require-checker: PRISM (prism) is not on PATH", file=sys.stderr)
        return 2
    if not (math.isfinite(args.timeout) and args.timeout > 0):
        print("Error: --timeout must be a positive finite number of seconds",
              file=sys.stderr)
        return 2
    try:
        report = check_file(args.file, args.contract, args.bindings, args.members,
                            args.step, args.horizon, args.export, timeout=args.timeout)
    except NormsEvalError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if args.format == "json":
        import json
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_text(report))
    if not report.get("checked"):
        print(f"norms-check: {report.get('skipped')}", file=sys.stderr)
        return 0
    if report["unconfirmed"]:
        return 3
    return 1 if report["findings"] else 0


def _cmd_agents_run(args: argparse.Namespace) -> int:
    """Exit status: 0 when the trace was replayed, whatever the decisions
    are; 2 on a usage error. The audit file is written only after the
    whole trace was replayed, so a failed run leaves no partial log."""
    import json
    from .agents_runtime import format_text, load_run_trace, run
    from .audit_log import AuditKeyError, chain_lines, epoch_text, file_sha256, read_key
    from .norms_eval import NormsEvalError, TraceError, load_version

    def no_audit() -> None:
        if args.audit is not None:
            print(f"No audit file was written: the run did not complete "
                  f"({args.audit} was not created or changed).", file=sys.stderr)

    key = None
    if args.audit_key is not None:
        if args.audit is None:
            print("Error: --audit-key needs --audit", file=sys.stderr)
            return 2
        try:
            key = read_key(args.audit_key)
        except AuditKeyError as e:
            print(f"Error: {e}", file=sys.stderr)
            no_audit()
            return 2
    try:
        if not Path(args.trace).exists():
            raise NormsEvalError(f"File not found: {args.trace}")
        sos = load_version(args.file)
        trace = load_run_trace(args.trace)
        result = {"file": str(args.file), **run(sos, trace)}
        log = None
        if args.audit is not None:
            log = "".join(line + "\n" for line in chain_lines(
                result["audit"], file_sha256(args.file), epoch_text(trace.epoch), key,
                trace.run_id, trace.approvals))
        report = (json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
                  if args.format == "json" else format_text(result))
    except TraceError as e:
        print(f"Trace error: {args.trace}: {e}", file=sys.stderr)
        no_audit()
        return 2
    except (NormsEvalError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        no_audit()
        return 2
    if args.audit is not None:
        try:
            args.audit.write_text(log, encoding="utf-8")
        except OSError as e:
            print(f"Error: cannot write the audit log: {e}", file=sys.stderr)
            return 2
    print(report)
    return 0


def _cmd_audit_verify(args: argparse.Namespace) -> int:
    """Exit status: 0 valid, 1 invalid, 2 usage error."""
    import json
    from .audit_log import AuditKeyError, file_sha256, format_verify_text, read_key, verify

    try:
        key = read_key(args.key) if args.key is not None else None
        source = None
        if args.source is not None:
            try:
                source = file_sha256(args.source)
            except OSError as e:
                raise AuditKeyError(f"cannot read {args.source}: {e.strerror or e}") from None
        if not args.audit.is_file():
            raise AuditKeyError(f"File not found: {args.audit}")
        result = verify(args.audit, key=key, source_sha256=source, allow_open=args.open,
                        run_id=args.run_id, anchor=args.anchor)
    except (AuditKeyError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_verify_text(result))
    return 0 if result["valid"] else 1


def _cmd_norms_diff(args: argparse.Namespace) -> int:
    """Exit status: 0 when nothing prevents the amendment, 1 on an
    error-level finding (a missing transform target, an invalid new
    version), 2 on a usage error."""
    from .norms_diff import diff_versions, format_diff_text
    from .norms_eval import NormsEvalError, load_version

    for path in (args.old, args.new):
        if not path.exists():
            print(f"Error: File not found: {path}", file=sys.stderr)
            return 2
    try:
        old = load_version(args.old)
    except NormsEvalError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    try:
        new = load_version(args.new)
    except NormsEvalError as e:
        # The new version cannot take effect: a finding, not a usage error.
        result = {"contracts": [], "invalid": [str(e)], "errors": [str(e)], "ok": False}
    else:
        try:
            result = diff_versions(old, new, args.contract)
        except NormsEvalError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
    result = {"old": str(args.old), "new": str(args.new), **result}

    if args.format == "json":
        import json
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_diff_text(result))
    return 0 if result["ok"] else 1


def _cmd_norms_indices(args: argparse.Namespace) -> int:
    """Exit status: 0 when the indices were reported (warnings of J-6 to
    J-8 do not fail), 2 on a usage error: no file, a parse or type-check
    error, or a file that does not declare grounding 0.1 with norms 0.1."""
    from .grounding import GroundingError, format_report_text, report
    from .parser import parse_file
    from .type_checker import type_check

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 2
    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 2
    try:
        result = report(sos, args.contract)
    except GroundingError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    check = type_check(sos)
    if not check.ok:
        print(f"Error: {path} has {len(check.errors)} type-check error(s):", file=sys.stderr)
        for error in check.errors:
            print(f"  {error}", file=sys.stderr)
        return 2
    result = {"file": str(path), **result}
    if args.format == "json":
        import json
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_report_text(result))
    return 0


def _load_stochastic_model(path: Path):
    """Parse and type-check a file and return its interpreted model: block
    (Appendix H), or None after printing why there is none."""
    from .parser import parse_file
    from .stochastic import analyse, stochastic_declared
    from .type_checker import type_check

    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return None
    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return None
    tc_result = type_check(sos)
    for w in tc_result.warnings:
        print(f"  {w}", file=sys.stderr)
    if not tc_result.ok:
        for e in tc_result.errors:
            print(f"  {e}", file=sys.stderr)
        print("Type check failed: the model is not interpreted", file=sys.stderr)
        return None
    if sos.model is None:
        print(f"Error: {path} has no model: block", file=sys.stderr)
        return None
    if not stochastic_declared(sos):
        print(f"Error: {path} does not declare 'stochastic: 0.1' under extensions:; "
              f"its model: block is not interpreted (Appendix H, H.15)", file=sys.stderr)
        return None
    return analyse(sos).model


def _cmd_model_export(args: argparse.Namespace) -> int:
    from .stochastic import ExportError, export_prism

    model = _load_stochastic_model(args.file)
    if model is None:
        return 1
    try:
        files = export_prism(model, args.regime)
    except ExportError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if model.kind == "smg":
        from .games import export_warnings
        for message in export_warnings(model):
            print(f"Warning: {message}", file=sys.stderr)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (args.output / name).write_text(text, encoding="utf-8")
        print(f"Exported: {args.output / name}")
    return 0


def _print_certified_map(args: argparse.Namespace, model, result: dict) -> int:
    """Print a certified regime map (H.9). Exit status 0 when the
    resolution is reached, 1 when it is not."""
    from .stochastic import decimal_text
    from .stochastic_eval import value_text

    status = 0 if result["resolution_reached"] else 1
    stopped = {
        "resolution": "the resolution is reached",
        "max_depth": f"no undecided cell may be bisected again (--max-depth {result['max_depth']})",
        "max_cells": f"the partition has {result['cells_before_merging']} cells "
                     f"(--max-cells {result['max_cells']})",
    }[result["stopped"]]
    if args.format == "json":
        import json
        output = {
            "file": str(args.file),
            "sos": model.sos_name,
            "regime_map": {
                "id": result["id"],
                "certified": True,
                "note": result["note"],
                "method": result["method"],
                "direction": result["direction"],
                "objective": result["objective"],
                "constraint": result["constraint"],
                "open": result["open"],
                "regimes": result["regimes"],
                "region": {n: [decimal_text(lo), decimal_text(hi)]
                           for n, (lo, hi) in result["region"].items()},
                "resolution": decimal_text(result["resolution"]),
                "undecided_fraction": str(result["undecided_fraction"]),
                "undecided_fraction_decimal": value_text(result["undecided_fraction"]),
                "resolution_reached": result["resolution_reached"],
                "stopped": result["stopped"],
                "budget": {"max_depth": result["max_depth"], "max_cells": result["max_cells"]},
                "counts": {
                    **result["counts"],
                    "cells": len(result["cells"]),
                    "cells_before_merging": result["cells_before_merging"],
                    "cells_analysed": result["cells_analysed"],
                    "deepest_bisection": result["deepest"],
                },
                "volume_fractions": {k: str(v) for k, v in result["fractions"].items()},
                "volume_fraction_by_best": {k: str(v)
                                            for k, v in result["fraction_by_best"].items()},
                "cells": [
                    {
                        "box": {n: [decimal_text(lo), decimal_text(hi)]
                                for n, (lo, hi) in c.box.items()},
                        "status": c.status,
                        "best": list(c.best),
                        **({"reason": c.reason} if c.reason else {}),
                    }
                    for c in result["cells"]
                ],
            },
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return status

    counts = result["counts"]
    print(f"Model check: {args.file}")
    print(f"  SoS: {model.sos_name}")
    print()
    print(f"--- Regime map '{result['id']}': {result['note']} ---")
    print(f"  method: {result['method']}")
    print(f"  objective: {result['direction']} {result['objective']}")
    if result["constraint"]:
        print(f"  constraint: {result['constraint']}")
    print(f"  regimes: {', '.join(result['regimes'])}")
    region = ", ".join(f"{n} in [{decimal_text(lo)}, {decimal_text(hi)}]"
                       for n, (lo, hi) in result["region"].items())
    print(f"  region: {region}")
    print(f"  cells: {len(result['cells'])} ({counts['decided']} decided, "
          f"{counts['infeasible']} infeasible, {counts['undecided']} undecided); "
          f"{result['cells_analysed']} cell(s) analysed, deepest bisection {result['deepest']}")
    for best, share in result["fraction_by_best"].items():
        print(f"  decided, best = {best}: {value_text(share)} of the region")
    if counts["infeasible"]:
        print(f"  infeasible: {value_text(result['fractions']['infeasible'])} of the region")
    print(f"  undecided fraction: {value_text(result['undecided_fraction'])} "
          f"(exact: {result['undecided_fraction']}); resolution: "
          f"{decimal_text(result['resolution'])}")
    if result["resolution_reached"]:
        print("  The resolution is reached.")
    else:
        print(f"  The resolution is NOT reached: {stopped}.")
    print()
    for c in result["cells"]:
        box = ", ".join(f"{n} in [{decimal_text(lo)}, {decimal_text(hi)}]"
                        for n, (lo, hi) in c.box.items())
        if c.status == "decided":
            what = f"decided, best = {', '.join(c.best)}"
        elif c.reason:
            what = f"{c.status} ({c.reason})"
        else:
            what = c.status
        print(f"  {box}: {what}")
    return status


def _cmd_model_check(args: argparse.Namespace) -> int:
    from .stochastic import decimal_text
    from .stochastic_eval import (
        ESTIMATE_NOTE, NOTE, EvaluationError, check_properties, exact_text,
        parse_number, regime_map_estimate, value_text,
    )

    # Exit status: 0 when every threshold property evaluated is satisfied
    # (queries do not count), 1 when at least one is violated, 2 when
    # nothing could be evaluated (usage error, or no interpreted model).
    # With --certify: 0 when the certified map reaches its resolution, 1
    # when it does not.
    model = _load_stochastic_model(args.file)
    if model is None:
        return 2
    strategic = model.kind == "smg"
    if strategic:
        # A property or regime map of a strategic model is evaluated in the
        # Markov chain of its profile (Appendix I, I.5).
        from .games import check_properties_game as check_properties  # noqa: F811
        from .games import model_of_regime_map
        if args.regime_map is not None:
            model = model_of_regime_map(model, args.regime_map)
    certified = None
    try:
        if args.certify and args.regime_map is None:
            raise EvaluationError("--certify needs --regime-map ID")
        if args.certify and args.grid is not None:
            raise EvaluationError(
                "--grid asks for estimates at sample points and --certify for a "
                "certified regime map; give one of them")
        if not args.certify and (args.max_depth is not None or args.max_cells is not None):
            raise EvaluationError("--max-depth and --max-cells need --certify")
        if args.certify and args.regime is not None:
            raise EvaluationError("--regime cannot be combined with --certify: the "
                                  "query lists the regimes it compares")
        overrides = {}
        for item in args.assignments:
            name, sep, value = item.partition("=")
            if not sep:
                raise EvaluationError(f"--set expects p=v, not '{item}'")
            overrides[name.strip()] = parse_number(value)
        if args.certify:
            from .stochastic_regions import (
                DEFAULT_MAX_CELLS, DEFAULT_MAX_DEPTH, certified_regime_map,
            )
            certified = certified_regime_map(
                model, args.regime_map, overrides,
                DEFAULT_MAX_DEPTH if args.max_depth is None else args.max_depth,
                DEFAULT_MAX_CELLS if args.max_cells is None else args.max_cells)
            estimate = None
            designs = []
        elif args.regime_map is not None:
            estimate = regime_map_estimate(
                model, args.regime_map, 4 if args.grid is None else args.grid, overrides)
            designs = []
        else:
            estimate = None
            designs = check_properties(model, args.regime, overrides)
    except EvaluationError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    status = 1 if any(p["result"] == "violated"
                      for d in designs for p in d["properties"]) else 0
    if certified is not None:
        return _print_certified_map(args, model, certified)

    if args.format == "json":
        import json
        output = {"file": str(args.file), "sos": model.sos_name, "evaluator": NOTE}
        if estimate is None:
            output["designs"] = [
                {
                    "design": d["design"],
                    "parameters": {k: decimal_text(v) for k, v in d["parameters"].items()},
                    "reachable_states": d["states"],
                    "properties": [
                        {"id": p["id"], "expr": p["expr"], "value": exact_text(p["value"]),
                         "decimal": value_text(p["value"]), "result": p["result"],
                         **({"profile": p["profile"]} if strategic else {})}
                        for p in d["properties"]
                    ],
                }
                for d in designs
            ]
        else:
            output["regime_map_estimate"] = {
                **{k: estimate[k] for k in ("id", "note", "direction", "objective",
                                            "constraint", "open", "regimes", "grid")},
                "points": [
                    {
                        "point": {k: decimal_text(v) for k, v in pt["point"].items()},
                        "regimes": {
                            r: {"value": exact_text(x["value"]),
                                "decimal": value_text(x["value"]),
                                "constraint": x["constraint"], "sat": x["sat"]}
                            for r, x in pt["regimes"].items()
                        },
                        "best_at_point": pt["best"],
                    }
                    for pt in estimate["points"]
                ],
            }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return status

    print(f"Model check: {args.file}")
    print(f"  SoS: {model.sos_name}")
    print(f"  Evaluator: {NOTE}")
    print()
    if estimate is None:
        for d in designs:
            params = ", ".join(f"{k}={decimal_text(v)}" for k, v in d["parameters"].items())
            print(f"--- Design {d['design']} ({params}; "
                  f"{d['states']} reachable state(s)) ---")
            for p in d["properties"]:
                result = f"  {p['result']}" if p["result"] else "  (query)"
                under = f"  (profile {p['profile']})" if strategic else ""
                print(f"  {p['id']}: {p['expr']}{under}")
                print(f"    value = {value_text(p['value'])}  (exact: "
                      f"{exact_text(p['value'])}){result}")
            if not d["properties"]:
                print("  (no property concerns this design)")
            print()
        return status

    print(f"--- Regime map query '{estimate['id']}': {ESTIMATE_NOTE} ---")
    print(f"  objective: {estimate['direction']} {estimate['objective']}")
    if estimate["constraint"]:
        print(f"  constraint: {estimate['constraint']}")
    print(f"  Each line is the exact evaluation at one sample point (grid of "
          f"{estimate['grid']} interval(s) per open parameter). No cell is decided.")
    for pt in estimate["points"]:
        at = ", ".join(f"{k}={decimal_text(v)}" for k, v in pt["point"].items())
        values = ", ".join(
            f"{r}={value_text(x['value'])}"
            + ("" if x["constraint"] else " (violates the constraint)")
            for r, x in pt["regimes"].items()
        )
        best = ", ".join(pt["best"]) if pt["best"] else "none (no regime satisfies the constraint)"
        print(f"  {at}: {values}; best at this point: {best}")
    return 0


def _cmd_game_check(args: argparse.Namespace) -> int:
    """Equilibrium checks and maps of a strategic model (Appendix I, I.13).

    Exit status: 0 when every requested result was computed, whatever the
    answers; 1 when a map did not reach its resolution or a query was not
    evaluated; 2 when nothing can be claimed (a static error, a usage
    error, a design that is not well-defined or a profile that is not
    admissible at the values used)."""
    from .games import NotEvaluated
    from .games_eval import (
        NOTE, EvaluationError, check_json, check_text, equilibrium_check,
        equilibrium_map, map_json, map_text,
    )
    from .stochastic_eval import parse_number
    from .stochastic_regions import DEFAULT_MAX_CELLS, DEFAULT_MAX_DEPTH

    model = _load_stochastic_model(args.file)
    if model is None:
        return 2
    checks, maps, skipped = [], [], []
    status = 0
    try:
        if model.kind != "smg":
            raise EvaluationError(
                f"the model of {args.file} is not a strategic model (kind: smg, "
                f"Appendix I); it has no equilibrium queries")
        map_options = (args.max_depth is not None or args.max_cells is not None
                       or args.partition is not None or args.no_merge)
        if map_options and not args.certify:
            raise EvaluationError(
                "--max-depth, --max-cells, --partition and --no-merge need --certify")
        if args.regime is not None and model.design(args.regime) is None:
            raise EvaluationError(
                f"'{args.regime}' is not a design of the file "
                f"(designs: {', '.join(d.name for d in model.designs)})")
        overrides = {}
        for item in args.assignments:
            name, sep, value = item.partition("=")
            if not sep:
                raise EvaluationError(f"--set expects p=v, not '{item}'")
            overrides[name.strip()] = parse_number(value)
        known = {q.id: q for q in model.equilibria}
        for name in args.equilibria:
            if name not in known:
                raise EvaluationError(
                    f"'{name}' is not an entry of model.equilibria "
                    f"(entries: {', '.join(known) or 'none'})")
            if known[name].is_map and not args.certify:
                raise EvaluationError(
                    f"'{name}' is an equilibrium map; computing it needs --certify")
        for query in model.equilibria:
            if args.equilibria and query.id not in args.equilibria:
                continue
            if query.is_map and not args.certify:
                skipped.append(query.id)
                continue
            try:
                if query.is_map:
                    result = equilibrium_map(
                        model, query.id, args.regime, overrides,
                        DEFAULT_MAX_DEPTH if args.max_depth is None else args.max_depth,
                        DEFAULT_MAX_CELLS if args.max_cells is None else args.max_cells,
                        args.partition, not args.no_merge)
                    if not all(d["resolution_reached"] for d in result["designs"]):
                        status = 1
                    maps.append(map_json(result) if args.format == "json" else result)
                else:
                    result = equilibrium_check(model, query.id, args.regime, overrides)
                    checks.append(check_json(result) if args.format == "json" else result)
            except NotEvaluated as e:
                # I.13: per design {design, status: not_evaluated, reason}.
                status = 1
                regimes = query.regimes if args.regime is None else \
                    [r for r in query.regimes if r == args.regime]
                entry = {"id": query.id, "profile": query.profile,
                         "designs": [{"design": r, "status": "not_evaluated",
                                      "reason": str(e)} for r in regimes]}
                (maps if query.is_map else checks).append(entry)
    except EvaluationError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    if args.format == "json":
        import json
        output = {"file": str(args.file), "sos": model.sos_name, "evaluator": NOTE,
                  "checks": checks, "maps": maps, "maps_not_computed": skipped}
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return status
    print(f"Game check: {args.file}")
    print(f"  SoS: {model.sos_name}")
    print(f"  Evaluator: {NOTE}")
    for result in checks + maps:
        print()
        if any(d.get("status") == "not_evaluated" for d in result["designs"]):
            print(f"--- Equilibrium query '{result['id']}': not_evaluated ---")
            for d in result["designs"]:
                print(f"  Design {d['design']}: not_evaluated: {d['reason']}")
            continue
        for line in (map_text(result) if "over" in result else check_text(result)):
            print(line)
    if skipped:
        print()
        print(f"Equilibrium maps not computed (give --certify): {', '.join(skipped)}")
    if not checks and not maps and not skipped:
        print()
        print("  (model.equilibria has no entry)")
    return status


def _cmd_ai(args: argparse.Namespace) -> int:
    from .ai import generate_cadl, ClientConfig

    # Get description
    description = args.description
    if args.input_file:
        if not args.input_file.exists():
            print(f"Error: File not found: {args.input_file}", file=sys.stderr)
            return 1
        description = args.input_file.read_text(encoding="utf-8")
    if not description:
        print("Error: Provide a description as argument or via -f <file>", file=sys.stderr)
        return 1

    # Configure client
    config = ClientConfig()
    if args.model:
        config.model = args.model

    # Generate
    print("Generating CADL from description...", file=sys.stderr)
    try:
        result = generate_cadl(description, config=config, verify=args.verify)
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Generation error: {e}", file=sys.stderr)
        return 1

    if result.errors:
        print("Warning: Generated CADL has validation issues:", file=sys.stderr)
        for err in result.errors:
            print(f"  {err}", file=sys.stderr)

    if result.retried:
        print("(retried after initial validation failure)", file=sys.stderr)

    # Output
    if args.output_file:
        args.output_file.write_text(result.cadl_source, encoding="utf-8")
        print(f"Saved to: {args.output_file}", file=sys.stderr)
    else:
        print(result.cadl_source)

    if result.sos:
        print(f"  SoS: {result.sos.name}", file=sys.stderr)
        print(f"  Actors: {len(result.sos.actors)}", file=sys.stderr)
        print(f"  Contracts: {len(result.sos.contracts)}", file=sys.stderr)
        print(f"  Protocols: {len(result.sos.protocols)}", file=sys.stderr)

    return 0 if not result.errors else 1


if __name__ == "__main__":
    sys.exit(main())
