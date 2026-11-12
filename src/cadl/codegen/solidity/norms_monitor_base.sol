// >>> norms-monitor-base
// The norms monitor of the Solidity target (Appendix F, F.5; Appendix D).
//
// This contract is the same for every CADL contract; `cadl codegen -t
// solidity` copies it verbatim. The generated contract derives from it and
// fills the model (the norms of the CADL contract as data) in its
// constructor; no text of the .cadl file is part of the code. The steps of
// one observation are those of the engine of `cadl norms-eval`
// (cadl/norms_eval.py, _Engine._step), in the same order.
//
// Numbers (values, request fields, bindings, the time t and durations) are
// fixed-point decimals: the int256 is the number times 10**6 (SCALE). The
// time of an observation is an argument of `observe`, in seconds times
// 10**6, so that a trace can be replayed; a deployment may pass
// block.timestamp * SCALE.
// Numbers, value kinds and binary operators
int256 constant SCALE = 1000000;
int256 constant MAX_T = 9007199254740992 * 1000000;

// kinds of a value
uint8 constant K_U = 0;   // unknown (no value); in an input: null
uint8 constant K_D = 1;   // undecided (later observations decide)
uint8 constant K_B = 2;   // boolean, n is 0 or 1
uint8 constant K_N = 3;   // number, n is the number times SCALE
uint8 constant K_T = 4;   // string
uint8 constant K_S = 5;   // a name without value (enum-like literal)

uint8 constant B_EQ = 0;
uint8 constant B_NE = 1;
uint8 constant B_LT = 2;
uint8 constant B_LE = 3;
uint8 constant B_GT = 4;
uint8 constant B_GE = 5;
uint8 constant B_ADD = 6;
uint8 constant B_SUB = 7;

struct Val { uint8 k; int256 n; string s; }
// a value of an observation, of the request or a binding (k: K_U for null)
struct ValIn { string name; uint8 k; int256 n; string s; }
struct Msg { string from; string to; string name; ValIn[] args; }

// operators of the nodes of a predicate; the index of an actor reference
uint8 constant OP_LIT = 0;
uint8 constant OP_NAME = 1;
uint8 constant OP_SYM = 2;
uint8 constant OP_MEM = 3;
uint8 constant OP_REQ = 4;
uint8 constant OP_NOT = 5;
uint8 constant OP_AND = 6;
uint8 constant OP_OR = 7;
uint8 constant OP_BIN = 8;
uint8 constant OP_IN = 9;
uint8 constant OP_WITHIN = 10;
uint8 constant OP_THROUGHOUT = 11;
uint8 constant OP_UNTIL = 12;
// the index of an actor reference
uint8 constant I_NONE = 0;
uint8 constant I_LIT = 1;
uint8 constant I_VAR = 2;
uint8 constant I_ANY = 3;

struct Node { uint8 op; uint8 bop; uint256 a; uint256 b; int256 d; uint8 ik; string s1; string s2; string s3; uint256[] items; Val lit; }
struct Stored { uint256 idx; uint8 k; int256 n; string s; }
// What the evaluation of predicates reads: the model's nodes and the
// observations of the contract instance.
// The scan of a temporal operator (F.4) from one start, advanced by the
// observations that arrive: its operand has no temporal operator, so its
// value at an observation is final when the observation is recorded.
struct Scan {
    bool started; uint256 next; bool hit; bool unknown;          // within, throughout
    bool prefixUnknown; bool uncertain; bool broken;              // until
}

struct EvalData {
    Node[] nodes;
    bool hasLifecycle;
    mapping(bytes32 => bool) hasBinding;
    mapping(bytes32 => Stored) bindings;
    mapping(bytes32 => bool) hasRequest;
    mapping(bytes32 => Stored) request;
    mapping(bytes32 => bool) known;
    mapping(bytes32 => Stored[]) history;
    mapping(bytes32 => uint256) head;     // history entries before it are dropped
    uint256 keepFrom;                     // no lookup reads an observation before it
    mapping(bytes32 => Scan) scans;       // incremental temporal operators, by (node, start)
    int256[] obsT;
    string[] obsSt;          // lifecycle state before each observation
    // while `over`, node overNode evaluates to overVal (the content of a
    // norm with its temporal operator replaced, F.5)
    bool over; uint256 overNode; Val overVal;
}

// Functions of the monitor without state, in a library of their own (the
// monitor would exceed the size limit of a contract, EIP-170, otherwise).
library CadlNormsValues {
    function splitActor(string memory actual) public pure returns (string memory name, bool hasIdx, string memory idx) {
        bytes memory b = bytes(actual);
        uint256 i = 0;
        while (i < b.length && b[i] != "[") i++;
        name = string(slice(b, 0, i));
        if (i < b.length && b[b.length - 1] == "]") {
            hasIdx = true;
            idx = string(slice(b, i + 1, b.length - 1));
        }
    }

    function slice(bytes memory b, uint256 from, uint256 to) internal pure returns (bytes memory out) {
        out = new bytes(to - from);
        for (uint256 i = from; i < to; i++) out[i - from] = b[i];
    }

    function parseIndex(string memory idx) public pure returns (int256 v) {
        bytes memory b = bytes(idx);
        for (uint256 i = 0; i < b.length; i++) v = v * 10 + int256(uint256(uint8(b[i])) - 48);
    }

    function isDigits(string memory s) public pure returns (bool) {
        bytes memory b = bytes(s);
        if (b.length == 0) return false;
        for (uint256 i = 0; i < b.length; i++) if (b[i] < "0" || b[i] > "9") return false;
        return true;
    }


    function checkBegin(ValIn[] calldata bindings_, ValIn[] calldata request_) public pure {
        for (uint256 i = 0; i < bindings_.length; i++) {
            ValIn calldata b = bindings_[i];
            require(b.k == K_T || (b.k == K_N && b.n % SCALE == 0), "a binding must be an integer or a string");
        }
        for (uint256 i = 0; i < request_.length; i++) {
            uint8 k = request_[i].k;
            require(k == K_U || k == K_B || k == K_N || k == K_T,
                    "a request field must be null, a boolean, a number or a string");
        }
    }

    function checkObservation(ValIn[] calldata values, Msg[] calldata msgs) public pure {
        checkMessages(msgs);
        for (uint256 i = 0; i < values.length; i++) {
            uint8 k = values[i].k;
            require(k == K_U || k == K_B || k == K_N || k == K_T,
                    "a value must be null, a boolean, a number or a string");
            require(k != K_B || values[i].n == 0 || values[i].n == 1, "a boolean is 0 or 1");
            requireAscii(values[i].name);
        }
    }

    function boolVal(bool b) internal pure returns (Val memory) {
        return Val(K_B, b ? int256(1) : int256(0), "");
    }

    function mark(uint8 k) internal pure returns (Val memory) {
        return Val(k, 0, "");
    }

    function isText(Val memory v) internal pure returns (bool) {
        return v.k == K_T || v.k == K_S;
    }

    function eqVal(Val memory a, Val memory b) internal pure returns (bool) {
        if ((a.k == K_B) != (b.k == K_B)) return false;
        if (isText(a) || isText(b)) return isText(a) && isText(b) && keccak256(bytes(a.s)) == keccak256(bytes(b.s));
        return a.n == b.n;
    }

    function compareText(string memory a, string memory b) internal pure returns (int256) {
        bytes memory x = bytes(a);
        bytes memory y = bytes(b);
        uint256 m = x.length < y.length ? x.length : y.length;
        for (uint256 i = 0; i < m; i++) {
            if (x[i] < y[i]) return -1;
            if (x[i] > y[i]) return 1;
        }
        return x.length < y.length ? int256(-1) : x.length > y.length ? int256(1) : int256(0);
    }

    function order(uint8 bop, int256 c) internal pure returns (Val memory) {
        if (bop == B_LT) return boolVal(c < 0);
        if (bop == B_LE) return boolVal(c <= 0);
        if (bop == B_GT) return boolVal(c > 0);
        return boolVal(c >= 0);
    }

    function symBad(uint8 bop, Val memory s, Val memory x) internal pure returns (bool) {
        return s.k == K_S && !((bop == B_EQ || bop == B_NE) && x.k == K_T);
    }

    function binop(uint8 bop, Val memory l, Val memory r) public pure returns (Val memory) {
        if (l.k == K_U || r.k == K_U) return mark(K_U);
        if (l.k == K_D || r.k == K_D) return mark(K_D);
        if (symBad(bop, l, r) || symBad(bop, r, l)) return mark(K_U);
        if (bop == B_EQ) return boolVal(eqVal(l, r));
        if (bop == B_NE) return boolVal(!eqVal(l, r));
        bool ln = l.k == K_N || l.k == K_B;
        bool rn = r.k == K_N || r.k == K_B;
        if (bop <= B_GE) {
            if (ln && rn) {
                int256 a = l.k == K_B ? l.n * SCALE : l.n;
                int256 b = r.k == K_B ? r.n * SCALE : r.n;
                return order(bop, a < b ? int256(-1) : a > b ? int256(1) : int256(0));
            }
            if (l.k == K_T && r.k == K_T) return order(bop, compareText(l.s, r.s));
            return mark(K_U);
        }
        if (l.k != K_N || r.k != K_N) return mark(K_U);
        if (bop == B_ADD) return Val(K_N, l.n + r.n, "");
        return Val(K_N, l.n - r.n, "");
    }

    function isAlpha(bytes1 c) internal pure returns (bool) {
        return (c >= "A" && c <= "Z") || (c >= "a" && c <= "z") || c == "_";
    }

    function isName(bytes memory b, uint256 from, uint256 to) internal pure returns (bool) {
        if (to <= from || !isAlpha(b[from])) return false;
        for (uint256 i = from + 1; i < to; i++) if (!isAlpha(b[i]) && !(b[i] >= "0" && b[i] <= "9")) return false;
        return true;
    }

    function isConcreteActor(string calldata s) internal pure returns (bool) {
        bytes memory b = bytes(s);
        uint256 i = 0;
        while (i < b.length && b[i] != "[") i++;
        if (!isName(b, 0, i)) return false;
        if (i == b.length) return true;
        if (b[b.length - 1] != "]" || b.length - i < 3) return false;
        if (b[i + 1] == "0" && b.length - i != 3) return false;
        for (uint256 j = i + 1; j < b.length - 1; j++) if (b[j] < "0" || b[j] > "9") return false;
        return true;
    }

    function checkMessages(Msg[] calldata msgs) public pure {
        for (uint256 m = 0; m < msgs.length; m++) {
            require(isConcreteActor(msgs[m].from) && isConcreteActor(msgs[m].to),
                    "'from' and 'to' of a message must be concrete actors such as PLANNER or ROBOT[2]");
            bytes memory nm = bytes(msgs[m].name);
            require(isName(nm, 0, nm.length), "'name' of a message must be a message name (an identifier)");
            for (uint256 a = 0; a < msgs[m].args.length; a++) {
                uint8 k = msgs[m].args[a].k;
                require(k == K_U || k == K_B || k == K_N || k == K_T, "an argument must be null, a boolean, a number or a string");
            }
        }
    }

    function requireAscii(string calldata s) public pure {
        bytes memory b = bytes(s);
        for (uint256 i = 0; i < b.length; i++) require(uint8(b[i]) < 0x80, "a value name must be ASCII");
    }

    /// A name without whitespace (the trace format of F.5)
    function canon(string memory s) public pure returns (string memory) {
        bytes memory b = bytes(s);
        bytes memory out = new bytes(b.length);
        uint256 c = 0;
        for (uint256 i = 0; i < b.length; i++) {
            uint8 x = uint8(b[i]);
            if (x == 0x20 || (x >= 0x09 && x <= 0x0d) || (x >= 0x1c && x <= 0x1f)) continue;
            out[c++] = b[i];
        }
        assembly { mstore(out, c) }
        return string(out);
    }

    function intText(int256 v) public pure returns (string memory) {
        if (v == 0) return "0";
        bool neg = v < 0;
        uint256 u = neg ? uint256(-v) : uint256(v);
        bytes memory out;
        while (u > 0) { out = abi.encodePacked(bytes1(uint8(48 + u % 10)), out); u /= 10; }
        return neg ? string.concat("-", string(out)) : string(out);
    }

}

// The evaluation of predicates (A.10 with the three-valued reading of F.5 and
// the temporal operators of F.4), in a library of its own.
library CadlNormsEval {
    function evaluate(EvalData storage data, uint256 id, uint256 p, uint256 n) public returns (Val memory) {
        return ev(data, id, p, n);
    }

    /// The truth of `root` at observation n with `node` read as `v`.
    function evaluateWith(EvalData storage data, uint256 root, uint256 n, uint256 node, Val memory v)
            public returns (Val memory r) {
        data.over = true;
        data.overNode = node;
        data.overVal = v;
        r = truth(ev(data, root, n, n));
        data.over = false;
    }

    function boolVal(bool b) internal pure returns (Val memory) {
        return Val(K_B, b ? int256(1) : int256(0), "");
    }

    function mark(uint8 k) internal pure returns (Val memory) {
        return Val(k, 0, "");
    }

    function isText(Val memory v) internal pure returns (bool) {
        return v.k == K_T || v.k == K_S;
    }

    function eqVal(Val memory a, Val memory b) internal pure returns (bool) {
        if ((a.k == K_B) != (b.k == K_B)) return false;
        if (isText(a) || isText(b)) return isText(a) && isText(b) && keccak256(bytes(a.s)) == keccak256(bytes(b.s));
        return a.n == b.n;
    }

    function truth(Val memory v) internal pure returns (Val memory) {
        if (v.k == K_U || v.k == K_D || v.k == K_B) return v;
        if (v.k == K_N) return boolVal(v.n != 0);
        return mark(K_U);
    }

    function stored(Stored storage s) internal view returns (Val memory) {
        if (s.k == K_U) return mark(K_U);
        return Val(s.k, s.n, s.s);
    }

    function lookup(EvalData storage data, string memory key, uint256 p) internal view returns (Val memory v, bool found) {
        bytes32 k = keccak256(bytes(key));
        Stored[] storage h = data.history[k];
        // the last entry at or before p: binary search over the kept entries
        uint256 lo = data.head[k];
        uint256 hi = h.length;
        if (lo >= hi || h[lo].idx > p) return (mark(K_U), false);
        while (hi - lo > 1) {
            uint256 mid = (lo + hi) / 2;
            if (h[mid].idx <= p) lo = mid; else hi = mid;
        }
        return (stored(h[lo]), true);
    }

    function nameVal(EvalData storage data, Node storage nd, uint256 p) internal view returns (Val memory) {
        bytes32 h = keccak256(bytes(nd.s1));
        if (data.hasBinding[h]) return stored(data.bindings[h]);
        if (h == keccak256("now")) return Val(K_N, data.obsT[p], "");
        if (h == keccak256("state") && data.hasLifecycle) return Val(K_T, 0, data.obsSt[p]);
        if (data.known[h]) { (Val memory v, ) = lookup(data, nd.s1, p); return v; }
        return Val(K_S, 0, nd.s1);
    }

    function memVal(EvalData storage data, Node storage nd, uint256 p) internal view returns (Val memory) {
        if (nd.ik == I_NONE) { (Val memory v, ) = lookup(data, string.concat(nd.s1, ".", nd.s3), p); return v; }
        (string memory text, ) = actorText(data, nd.s1, nd.ik, nd.s2);
        (Val memory w, bool found) = lookup(data, string.concat(text, ".", nd.s3), p);
        if (found) return w;
        (Val memory x, ) = lookup(data, string.concat(nd.s1, ".", nd.s3), p);
        return x;
    }

    function ev(EvalData storage data, uint256 id, uint256 p, uint256 n) internal returns (Val memory) {
        if (data.over && id == data.overNode) return data.overVal;
        Node storage nd = data.nodes[id];
        uint8 op = nd.op;
        if (op == OP_LIT) return nd.lit;
        if (op == OP_NAME) return nameVal(data, nd, p);
        if (op == OP_SYM) { (string memory text, ) = actorText(data, nd.s1, nd.ik, nd.s2); return Val(K_S, 0, text); }
        if (op == OP_MEM) return memVal(data, nd, p);
        if (op == OP_REQ) {
            bytes32 h = keccak256(bytes(nd.s1));
            if (!data.hasRequest[h]) return mark(K_U);
            return stored(data.request[h]);
        }
        if (op == OP_NOT) {
            Val memory v = truth(ev(data, nd.a, p, n));
            if (v.k != K_B) return v;
            return boolVal(v.n == 0);
        }
        if (op == OP_AND || op == OP_OR) return andOr(op == OP_OR, truth(ev(data, nd.a, p, n)), truth(ev(data, nd.b, p, n)));
        if (op == OP_BIN) return CadlNormsValues.binop(nd.bop, ev(data, nd.a, p, n), ev(data, nd.b, p, n));
        if (op == OP_IN) return inList(data, nd, p, n);
        if (op == OP_WITHIN || op == OP_THROUGHOUT) return temporal(data, id, nd, p, n);
        return untilOp(data, id, nd, p, n);
    }

    function andOr(bool isOr, Val memory l, Val memory r) internal pure returns (Val memory) {
        int256 stop = isOr ? int256(1) : int256(0);
        if (l.k == K_B && l.n == stop) return l;
        if (r.k == K_B && r.n == stop) return r;
        if (l.k == K_D || r.k == K_D) return mark(K_D);
        if (l.k == K_U || r.k == K_U) return mark(K_U);
        return boolVal(!isOr);
    }

    function inList(EvalData storage data, Node storage nd, uint256 p, uint256 n) internal returns (Val memory) {
        Val memory l = ev(data, nd.a, p, n);
        if (l.k == K_U || l.k == K_D) return l;
        if (l.k == K_S) return mark(K_U);
        bool sawD = false;
        bool sawU = false;
        for (uint256 i = 0; i < nd.items.length; i++) {
            Val memory it = ev(data, nd.items[i], p, n);
            if (it.k == K_S && l.k != K_T) it = mark(K_U);
            if (it.k == K_D) { sawD = true; continue; }
            if (it.k == K_U) { sawU = true; continue; }
            if (eqVal(l, it)) return boolVal(true);
        }
        if (sawD) return mark(K_D);
        if (sawU) return mark(K_U);
        return boolVal(false);
    }

    function worst(Val memory acc, Val memory v) internal pure returns (Val memory) {
        if (acc.k == K_D || v.k == K_D) return mark(K_D);
        if (acc.k == K_U || v.k == K_U) return mark(K_U);
        return acc;
    }

    function scanOf(EvalData storage data, uint256 id, uint256 p) internal returns (Scan storage sc) {
        sc = data.scans[keccak256(abi.encode(id, p))];
        if (!sc.started) { sc.started = true; sc.next = p; }
    }

    /// within (decisive true) and throughout (decisive false), F.4. The
    /// window is scanned once: each call reads only the new observations.
    function temporal(EvalData storage data, uint256 id, Node storage nd, uint256 p, uint256 n) internal returns (Val memory) {
        int256 dec = nd.op == OP_WITHIN ? int256(1) : int256(0);
        int256 limit = data.obsT[p] + nd.d;
        Scan storage sc = scanOf(data, id, p);
        while (!sc.hit && sc.next <= n && data.obsT[sc.next] <= limit) {
            Val memory v = truth(ev(data, nd.a, sc.next, n));
            if (v.k == K_B && v.n == dec) sc.hit = true;
            else if (v.k != K_B) sc.unknown = true;
            sc.next++;
        }
        if (sc.hit) return boolVal(dec == 1);
        if (data.obsT[n] <= limit) return mark(K_D);
        if (sc.unknown) return mark(K_U);
        return boolVal(dec == 0);
    }

    function untilOp(EvalData storage data, uint256 id, Node storage nd, uint256 p, uint256 n) internal returns (Val memory) {
        int256 limit = data.obsT[p] + nd.d;
        Scan storage sc = scanOf(data, id, p);
        while (!sc.hit && !sc.broken && sc.next <= n && data.obsT[sc.next] <= limit) {
            Val memory qv = truth(ev(data, nd.b, sc.next, n));
            if (qv.k == K_B && qv.n == 1) {
                if (!sc.prefixUnknown) { sc.hit = true; break; }
                sc.uncertain = true;
            } else if (qv.k != K_B) {
                sc.uncertain = true;
            }
            Val memory pv = truth(ev(data, nd.a, sc.next, n));
            if (pv.k == K_B && pv.n == 0) sc.broken = true;
            else if (pv.k != K_B) sc.prefixUnknown = true;
            sc.next++;
        }
        if (sc.hit) return boolVal(true);
        if (!sc.broken && data.obsT[n] <= limit) return mark(K_D);
        if (sc.uncertain) return mark(K_U);
        return boolVal(false);
    }

    /// The text of an actor reference with its index bound; bound is false
    /// for an index name that the instance does not bind.
    function actorText(EvalData storage data, string memory name, uint8 ik, string memory idx) internal view returns (string memory, bool) {
        if (ik == I_NONE) return (name, true);
        if (ik == I_LIT) return (string.concat(name, "[", idx, "]"), true);
        bytes32 h = keccak256(bytes(idx));
        if (data.hasBinding[h]) {
            Stored storage b = data.bindings[h];
            string memory text = b.k == K_T ? b.s : CadlNormsValues.intText(b.n / SCALE);
            return (string.concat(name, "[", text, "]"), true);
        }
        return (string.concat(name, "[", idx, "]"), false);
    }

}


// The candidates of a norm whose content is one temporal operator (F.4,
// F.5): the observations at which the content has to be decided, oldest
// first, with what the observations since the last judgement say about
// the operand. Each candidate is decided from these summaries; no
// window is scanned per candidate, so the cost of an observation does
// not grow with the number of candidates.
struct Window {
    uint256 head; uint256 tail; uint256 purge;     // candidates [head, tail); entries before purge are deleted
    bool luSet; uint256 lu;                        // candidates at or before lu have an operand that was unknown
    bool ffSet; int256 ff; bool fuSet; int256 fu;  // throughout: the first time false, unknown
    bool ftSet; int256 ft;                         // within: the first time true
    bool jSet; uint256 j; int256 jt;               // until: the first observation with q true
    bool pSet; uint256 pFirst; bool pFalse; bool pUnknown;   // until: p not true
    uint256 at; uint8 pv; uint8 qv;                // the operands at observation at - 1
    mapping(uint256 => uint256) k;                 // the observation of a candidate
    mapping(uint256 => int256) t;                  // its time
}

uint8 constant W_WITHIN = 1;
uint8 constant W_THROUGHOUT = 2;
uint8 constant W_UNTIL = 3;

// The temporal operator of the content of a norm: its kind (with `neg`,
// the dual operator on the negated operand: NOT within(d, p) is
// throughout(d, NOT p) and NOT throughout(d, p) is within(d, NOT p)), its
// operands (p, and q of until), its duration, the operator node and the
// root of the content.
struct Temporal { uint8 mode; bool neg; uint256 tp; uint256 tq; int256 td; uint256 node; uint256 root; }

/// The judgement of the candidates of a temporal content, with a cost per
/// observation that does not depend on the number of candidates: a few
/// binary searches over them, a few deletions of decided ones.
///
/// The rest of the content is decided at the observation of a candidate
/// (its names are fixed, see makeKnown), so a candidate is one of: true
/// whatever the operator gives (a hit), never true (not kept), true
/// exactly when the operator (or its dual) is true (`live`), or never
/// true but undecided until the operator is decided (`dead`: it keeps
/// the norm pending, as F.5 does).
library CadlNormsWindows {
    // truth values of an operand
    uint8 internal constant F = 0;
    uint8 internal constant T = 1;
    uint8 internal constant U = 2;
    // what a new candidate is
    uint8 internal constant C_HIT = 0;
    uint8 internal constant C_DROP = 1;
    uint8 internal constant C_LIVE = 2;
    uint8 internal constant C_DEAD = 3;

    /// What an observation says about the operands, for the candidates
    /// there are (also while the norm is suspended).
    function note(Window storage live, Window storage dead, EvalData storage data,
                  Temporal storage tm, uint256 n, int256 t) public {
        if (live.head == live.tail && dead.head == dead.tail) return;
        (uint8 pv, uint8 qv) = operands(live, data, tm, n);
        summarize(live, tm.mode, n, t, pv, qv);
        summarize(dead, tm.mode, n, t, pv, qv);
    }

    function summarize(Window storage w, uint8 mode, uint256 n, int256 t, uint8 pv, uint8 qv) internal {
        if (w.head == w.tail) return;
        if (mode == W_THROUGHOUT) {
            if (pv == F && !w.ffSet) { w.ffSet = true; w.ff = t; }
            if (pv == U && !w.fuSet) { w.fuSet = true; w.fu = t; }
        } else if (mode == W_WITHIN) {
            if (pv == T && !w.ftSet) { w.ftSet = true; w.ft = t; }
        } else {
            if (qv == T && !w.jSet) { w.jSet = true; w.j = n; w.jt = t; }
            if (pv != T && !w.pSet) { w.pSet = true; w.pFirst = n; }
            if (pv == F) w.pFalse = true;
            if (pv == U) w.pUnknown = true;
        }
    }

    /// The judgement of observation n (F.5, items 2 and 3) when the norm
    /// is active: whether a candidate holds (hit), and whether none is left
    /// (empty). `open`: the deadline has not passed, so n is a candidate.
    function judge(Window storage live, Window storage dead, EvalData storage data,
                   Temporal storage tm, uint256 n, int256 t, bool open) public returns (bool hit, bool empty) {
        (uint8 pv, uint8 qv) = operands(live, data, tm, n);
        // the operator is true at n for a candidate of n itself
        bool trueNow = (tm.mode == W_WITHIN && pv == T) || (tm.mode == W_UNTIL && qv == T);
        uint8 cls = open ? classify(data, tm, n) : C_DROP;
        hit = cls == C_HIT || (cls == C_LIVE && trueNow)
            || (live.head < live.tail && oldHit(live, tm.mode, tm.td, t));
        if (hit) {
            live.head = live.tail;
            dead.head = dead.tail;
        } else {
            advance(live, tm, n, t, pv, cls == C_LIVE, false);
            advance(dead, tm, n, t, pv, cls == C_DEAD && !trueNow, true);
        }
        reset(live);
        reset(dead);
        empty = live.head == live.tail && dead.head == dead.tail;
    }

    /// The candidates after an observation without a hit: false in every
    /// window (throughout: an operand false; until: p false) removes them
    /// all, an unknown operand marks them, the windows that are over go;
    /// then the new candidate (unless the operator is decided at it).
    function advance(Window storage w, Temporal storage tm, uint256 n, int256 t, uint8 pv, bool add,
                     bool isDead) internal {
        uint8 mode = tm.mode;
        int256 d = tm.td;
        if (isDead && mode == W_UNTIL && w.head < w.tail && w.jSet && (!w.pSet || w.j <= w.pFirst)) {
            // until is decided (true) for the clean candidates whose window has j: never
            // true for a dead candidate, so they go
            uint256 from = firstFrom(w, w.jt - d);
            uint256 firstClean = firstAfterK(w, w.luSet, w.lu);
            uint256 cut = from > firstClean ? from : firstClean;
            if (cut < w.tail) w.tail = cut;
        }
        if (w.head < w.tail) {
            if ((mode == W_THROUGHOUT && w.ffSet) || (mode == W_UNTIL && w.pFalse)
                    || (mode == W_WITHIN && w.ftSet)) {
                w.head = w.tail;
            } else {
                if ((mode == W_THROUGHOUT && w.fuSet) || (mode == W_UNTIL && w.pUnknown)) {
                    w.luSet = true;
                    w.lu = w.k[w.tail - 1];
                }
                w.head = firstFrom(w, t - d);
            }
        }
        if (add && !((mode == W_THROUGHOUT || mode == W_UNTIL) && pv == F)) {
            if ((mode == W_THROUGHOUT || mode == W_UNTIL) && pv == U) { w.luSet = true; w.lu = n; }
            w.k[w.tail] = n;
            w.t[w.tail] = t;
            w.tail++;
        }
    }

    function reset(Window storage w) internal {
        w.ffSet = false; w.fuSet = false; w.ftSet = false;
        w.jSet = false; w.pSet = false; w.pFalse = false; w.pUnknown = false;
        for (uint256 x = 0; x < 4 && w.purge < w.head; x++) {
            delete w.k[w.purge];
            delete w.t[w.purge];
            w.purge++;
        }
    }

    /// What candidate n is, from the content with the operator read as
    /// undecided, true and false (F.5: an undefined content is false).
    function classify(EvalData storage data, Temporal storage tm, uint256 n) internal returns (uint8) {
        if (tm.root == tm.node) return C_LIVE;
        Val memory g = CadlNormsEval.evaluateWith(data, tm.root, n, tm.node, Val(K_D, 0, ""));
        if (g.k == K_B) return g.n == 1 ? C_HIT : C_DROP;
        if (g.k != K_D) return C_DROP;
        // the operator itself is true for a hit of the window when not neg
        g = CadlNormsEval.evaluateWith(data, tm.root, n, tm.node, Val(K_B, tm.neg ? int256(0) : int256(1), ""));
        return g.k == K_B && g.n == 1 ? C_LIVE : C_DEAD;
    }

    /// The operands at observation n (0 false, 1 true, 2 unknown), once per
    /// observation; with neg, p is negated (Kleene).
    function operands(Window storage w, EvalData storage data, Temporal storage tm, uint256 n)
            internal returns (uint8 pv, uint8 qv) {
        if (w.at == n + 1) return (w.pv, w.qv);
        pv = code(CadlNormsEval.evaluate(data, tm.tp, n, n));
        if (tm.neg && pv != U) pv = pv == T ? F : T;
        qv = tm.mode == W_UNTIL ? code(CadlNormsEval.evaluate(data, tm.tq, n, n)) : 0;
        w.at = n + 1;
        w.pv = pv;
        w.qv = qv;
    }

    function code(Val memory v) internal pure returns (uint8) {
        if (v.k == K_B) return v.n == 1 ? T : F;
        if (v.k == K_N) return v.n != 0 ? T : F;
        return U;
    }

    function clean(Window storage w, uint256 i) internal view returns (bool) {
        return !w.luSet || w.k[i] > w.lu;
    }

    /// Whether a candidate from before this observation holds.
    function oldHit(Window storage w, uint8 mode, int256 d, int256 t) internal view returns (bool) {
        uint256 last = w.tail - 1;
        if (mode == W_WITHIN) return w.ftSet && w.t[last] + d >= w.ft;
        if (mode == W_UNTIL) {
            return w.jSet && (!w.pSet || w.j <= w.pFirst) && clean(w, last) && w.t[last] + d >= w.jt;
        }
        // throughout: the newest candidate whose window ended before the
        // first value that is not true, and before now
        int256 m = t;
        if (w.ffSet && w.ff < m) m = w.ff;
        if (w.fuSet && w.fu < m) m = w.fu;
        uint256 i = firstFrom(w, m - d);                    // first with t_k + d >= m
        return i > w.head && clean(w, i - 1);
    }

    /// The first candidate in [head, tail) with t_k >= x, or tail.
    function firstFrom(Window storage w, int256 x) internal view returns (uint256) {
        uint256 lo = w.head;
        uint256 hi = w.tail;
        while (lo < hi) {
            uint256 mid = (lo + hi) / 2;
            if (w.t[mid] < x) lo = mid + 1; else hi = mid;
        }
        return lo;
    }

    /// The first candidate in [head, tail) with k > lu (all when !luSet).
    function firstAfterK(Window storage w, bool luSet, uint256 lu) internal view returns (uint256) {
        if (!luSet) return w.head;
        uint256 lo = w.head;
        uint256 hi = w.tail;
        while (lo < hi) {
            uint256 mid = (lo + hi) / 2;
            if (w.k[mid] <= lu) lo = mid + 1; else hi = mid;
        }
        return lo;
    }
}

// The assume: and guarantee: items (value: 0 not decided, 1 true, 2 false,
// 3 undefined), in a library of their own.
struct Item { string text; bool temporal; bool broken; bool unsupported; uint256 root; }
struct ItemRec { uint8 value; bool seen; uint8 verdict; }

library CadlNormsItems {
    /// assume: before guarantee: (an assumption that fails excuses a
    /// guarantee that fails at the same observation). Returns whether an
    /// assumption has failed, now or before.
    function step(Item[] storage a, ItemRec[] storage ar, Item[] storage g, ItemRec[] storage gr,
                  EvalData storage data, uint256 n, bool failed) public returns (bool) {
        for (uint256 i = 0; i < a.length; i++) {
            if (itemStep(a[i], ar[i], data, n) == 2) failed = true;
        }
        for (uint256 i = 0; i < g.length; i++) {
            if (itemStep(g[i], gr[i], data, n) == 2) gr[i].verdict = failed ? 3 : 2;
        }
        return failed;
    }

    /// 2 when the item has just been found false
    function itemStep(Item storage it, ItemRec storage r, EvalData storage data, uint256 n) internal returns (uint8) {
        if (it.broken || it.unsupported || r.value != 0) return 0;
        if (it.temporal) {
            Val memory v = CadlNormsEval.evaluate(data, it.root, 0, n);
            if (v.k == K_D) return 0;
            if (v.k != K_B && v.k != K_N) { r.value = 3; return 0; }
            r.value = v.n != 0 ? 1 : 2;
            return r.value;
        }
        Val memory w = CadlNormsEval.evaluate(data, it.root, n, n);
        if ((w.k == K_B || w.k == K_N) && w.n != 0) { r.seen = true; return 0; }
        if (w.k == K_B || w.k == K_N) { r.value = 2; return 2; }
        return 0;
    }

    function finish(Item[] storage a, ItemRec[] storage ar, Item[] storage g, ItemRec[] storage gr,
                    bool endHere) public {
        for (uint256 i = 0; i < a.length; i++) finishItem(a[i], ar[i], endHere);
        for (uint256 i = 0; i < g.length; i++) finishItem(g[i], gr[i], endHere);
    }

    function finishItem(Item storage it, ItemRec storage r, bool endHere) internal {
        if (it.broken || it.unsupported) return;
        if (r.value == 0 && !it.temporal && endHere && r.seen) r.value = 1;
        if (r.value == 1) r.verdict = 1;
    }

    /// Whether a temporal item is not decided (it reads the first observation).
    function pending(Item[] storage a, ItemRec[] storage ar, Item[] storage g, ItemRec[] storage gr)
            public view returns (bool) {
        return pendingIn(a, ar) || pendingIn(g, gr);
    }

    function pendingIn(Item[] storage items, ItemRec[] storage rs) internal view returns (bool) {
        for (uint256 i = 0; i < items.length; i++) {
            if (items[i].temporal && !items[i].broken && !items[i].unsupported && rs[i].value == 0) return true;
        }
        return false;
    }
}

abstract contract CadlNormsMonitorBase {
    // states and verdicts of a norm instance
    uint8 internal constant INACTIVE = 0;
    uint8 internal constant ACTIVE = 1;
    uint8 internal constant SUSPENDED = 2;
    uint8 internal constant FULFILLED = 3;
    uint8 internal constant VIOLATED = 4;
    uint8 internal constant DISCHARGED = 5;
    uint8 internal constant V_PENDING = 0;
    uint8 internal constant V_FULFILLED = 1;
    uint8 internal constant V_VIOLATED = 2;
    uint8 internal constant V_EXCUSED = 3;

    // kinds of norms and terms; node operators
    uint8 internal constant OBLIGATION = 0;
    uint8 internal constant PERMISSION = 1;
    uint8 internal constant PROHIBITION = 2;
    uint8 internal constant POWER = 3;
    uint8 internal constant T_EVENT = 1;
    uint8 internal constant T_MESSAGE = 2;
    uint8 internal constant T_PREDICATE = 3;
    uint8 internal constant T_ERROR = 4;


    struct Pat { string name; uint8 ik; string idx; bool hasRange; int256 lo; int256 hi; }
    struct Term { uint8 kind; string text; uint256 root; Pat sender; Pat receiver; string msgName; bool hasArgs; uint256[] args; }
    struct NormDef {
        string id; uint8 kind; bool frozen; int256 trigger; int256 until; int256 content;
        bool hasDeadline; int256 deadline; bool target; bool hasBearer; Pat bearer;
        string severity; bool hasSanction; string sanction; int256 reparation;
        bool hasReparationId; string reparationId; bool hasTransition; string transition;
        uint256[] activate; uint256[] suspend; uint256[] resume; uint256[] discharge;
        // a content with a temporal operator (see `temporals`), else 0
        uint8 tmode;
    }
    struct Rule { string creates; bool broken; int256 when; int256 cond; }
    struct ActorDecl { bool declared; bool isIndexed; bool hasLo; int256 lo; bool hasHi; int256 hi; }

    struct NormRec {
        uint8 state; uint8 verdict; bool hasAt; int256 activatedAt; int256 act; bool hasSince;
        int256 since; int256 before; uint256 from;
    }
    struct Ev { string name; bool hasPerformer; string performer; string source; }
    struct Act { string content; string performer; string desc; }

    // --- the model (filled by the constructor of the generated contract) -----------
    string public contractId;
    EvalData internal data;
    Term[] internal terms;
    NormDef[] internal normDefs;
    Rule[] internal rules;
    Item[] internal assumeItems;
    Item[] internal guaranteeItems;
    mapping(bytes32 => ActorDecl) internal actors;
    string internal lcInitial;
    mapping(bytes32 => bool) internal lcStates;
    mapping(bytes32 => bool) internal lcTerminal;
    // the bare names read inside a temporal operator (see makeKnown)
    mapping(bytes32 => bool) internal temporalName;
    // by norm, for a content with a temporal operator: the operator, and
    // the candidates that are true with it and those that are never true
    mapping(uint256 => Temporal) internal temporals;
    mapping(uint256 => Window) internal windows;
    mapping(uint256 => Window) internal deadWindows;

    // --- who may record the instance -------------------------------------------------
    /// Only this address may call begin, observe, addQuantities and finish.
    address public reporter;

    modifier onlyReporter() {
        require(msg.sender == reporter, "only the reporter records observations");
        _;
    }

    /// The reporter is `reporter_`, or the deployer when it is the zero address.
    constructor(address reporter_) {
        reporter = reporter_ == address(0) ? msg.sender : reporter_;
    }

    // --- the state of the contract instance ----------------------------------------
    uint256 public observations;      // observations of the instance so far
    int256 public lastT;
    bool internal hasLastT;
    bool public ended;
    uint256 public ignored;           // observations after the end
    bool internal begun;
    string internal lc;               // lifecycle state after the last observation
    NormRec[] internal recs;
    ItemRec[] internal assumeRecs;
    ItemRec[] internal guaranteeRecs;
    bool internal failed;             // an assumption has failed
    int256 internal failedAt;

    event NormChanged(uint256 index, string id, uint8 state, uint8 verdict, uint8 previousState, uint8 previousVerdict, int256 t);
    event ItemChanged(bool guarantee, uint256 index, uint8 mark, uint8 previous, int256 t);
    event Violation(string norm, string severity, uint8 verdict, string sanction, string reparation, bool reparationActivated, string transition, int256 t);
    event InstitutionalEvent(string name, string performer, string source, int256 t);
    event PowerExercised(string power, string performer, string act, int256 t);
    event Unauthorised(string act, string performer, int256 t);

    // =========================================================================
    // The interface
    // =========================================================================

    /// Bindings, data.request fields (by path, "a.b") and names of quantities,
    /// before the first observation. Optional; may be called once.
    function begin(ValIn[] calldata bindings_, ValIn[] calldata request_, string[] calldata quantities) external onlyReporter {
        require(observations == 0 && !begun && !ended, "begin() comes before the first observation");
        begun = true;
        CadlNormsValues.checkBegin(bindings_, request_);
        for (uint256 i = 0; i < bindings_.length; i++) {
            ValIn calldata b = bindings_[i];
            bytes32 h = keccak256(bytes(b.name));
            data.hasBinding[h] = true;
            data.bindings[h] = Stored(0, b.k, b.n, b.s);
        }
        for (uint256 i = 0; i < request_.length; i++) {
            ValIn calldata r = request_[i];
            bytes32 h = keccak256(bytes(r.name));
            data.hasRequest[h] = true;
            data.request[h] = Stored(0, r.k, r.n, r.s);
        }
        for (uint256 i = 0; i < quantities.length; i++) {
            data.known[keccak256(bytes(CadlNormsValues.canon(quantities[i])))] = true;
        }
    }

    /// Names of quantities written without an actor that get a value later.
    function addQuantities(string[] calldata quantities) external onlyReporter {
        for (uint256 i = 0; i < quantities.length; i++) {
            makeKnown(keccak256(bytes(CadlNormsValues.canon(quantities[i]))));
        }
    }

    /// A name becomes a known quantity (F.5). The scans of the temporal
    /// operators keep the values they have read; a bare name read inside a
    /// temporal operator as a symbol would be read as a quantity from now
    /// on, which would change values already read. Such a name must be
    /// given in begin() (or be observed at the first observation): later
    /// the call is refused.
    function makeKnown(bytes32 h) internal {
        if (data.known[h]) return;
        require(observations == 0 || !temporalName[h] || data.hasBinding[h],
                "a name used inside a temporal operator becomes a quantity after the first observation; give it in begin()");
        data.known[h] = true;
    }

    /// One observation (F.5): its time (seconds * SCALE), the values that
    /// change, the messages sent, the lifecycle state after it (when
    /// hasState) and whether the instance ends at it. Reverts, leaving the
    /// monitor as it was, for an observation that is malformed.
    function observe(int256 t, ValIn[] calldata values, Msg[] calldata msgs,
                     bool hasState, string calldata state, bool end) external onlyReporter {
        checkInput(t, values, msgs);
        lastT = t;
        hasLastT = true;
        if (ended) {
            ignored += 1;
            return;
        }
        bool endHere = record(t, values, hasState, state, end);
        runStep(observations - 1, t, msgs, endHere);
    }

    function checkInput(int256 t, ValIn[] calldata values, Msg[] calldata msgs) internal view {
        require(t >= -MAX_T && t <= MAX_T, "'t' must be a finite number of at most 2**53 in absolute value");
        require(!hasLastT || t >= lastT, "time is before the previous observation");
        CadlNormsValues.checkObservation(values, msgs);
    }

    function record(int256 t, ValIn[] calldata values, bool hasState, string calldata state, bool end)
            internal returns (bool) {
        uint256 n = observations;
        string memory before = n == 0 ? lcInitial : lc;
        string memory after_ = hasState ? state : before;
        if (data.hasLifecycle) {
            require(lcStates[keccak256(bytes(after_))], "the state is not a state of the lifecycle");
        }
        for (uint256 i = 0; i < values.length; i++) {
            bytes32 h = keccak256(bytes(CadlNormsValues.canon(values[i].name)));
            makeKnown(h);
            prune(h);
            data.history[h].push(Stored(n, values[i].k, values[i].n, values[i].s));
        }
        data.obsT.push(t);
        data.obsSt.push(before);
        lc = after_;
        observations = n + 1;
        return end || (data.hasLifecycle && lcTerminal[keccak256(bytes(after_))]);
    }

    function runStep(uint256 n, int256 t, Msg[] calldata msgs, bool endHere) internal {
        (uint8[] memory ns, uint8[] memory nv, uint8[] memory am, uint8[] memory gm) = marks();
        step(n, t, msgs, endHere);
        emitChanges(ns, nv, am, gm, t);
        updateKeepFrom(n);
    }

    /// The first observation that a later step can read: this one, and the
    /// first while a temporal item is not decided (the candidates of a
    /// temporal content keep what they need in their window).
    function updateKeepFrom(uint256 n) internal {
        uint256 k = n;
        if (k > 0 && CadlNormsItems.pending(assumeItems, assumeRecs, guaranteeItems, guaranteeRecs)) k = 0;
        data.keepFrom = k;
    }


    /// Drop values of a quantity that no lookup can read any more: at most
    /// four per new value, so that the cost of an observation stays bounded
    /// when a long window closes (the rest goes with the next values).
    function prune(bytes32 h) internal {
        Stored[] storage hs = data.history[h];
        uint256 first = data.head[h];
        uint256 stop = first + 4;
        while (first < stop && first + 1 < hs.length && hs[first + 1].idx <= data.keepFrom) {
            delete hs[first];
            first++;
        }
        data.head[h] = first;
    }

    /// The end of the contract instance at its last observation (F.5,
    /// item 6), when no observation said so.
    function finish() external onlyReporter {
        require(observations > 0, "the contract instance has no observation");
        if (ended) {
            return;
        }
        (uint8[] memory ns, uint8[] memory nv, uint8[] memory am, uint8[] memory gm) = marks();
        uint256 n = observations - 1;
        settle(n, data.obsT[n]);
        finishItems(true, data.obsT[n]);
        emitChanges(ns, nv, am, gm, data.obsT[n]);
    }

    /// The states and verdicts of the norms (in the order of the contract),
    /// the status of the assume: items (0 pending, 1 holds, 2 failed) and the
    /// verdicts of the guarantee: items (0 pending, 1 fulfilled, 2 violated,
    /// 3 excused).
    function verdicts() external view returns (uint8[] memory normStates, uint8[] memory normVerdicts,
                                               uint8[] memory assumptions, uint8[] memory guarantees) {
        return marks();
    }

    function normIds() external view returns (string[] memory ids) {
        ids = new string[](normDefs.length);
        for (uint256 i = 0; i < normDefs.length; i++) ids[i] = normDefs[i].id;
    }

    /// False when a norm or a guarantee is violated.
    function ok() external view returns (bool) {
        for (uint256 i = 0; i < recs.length; i++) if (recs[i].verdict == V_VIOLATED) return false;
        for (uint256 i = 0; i < guaranteeRecs.length; i++) if (guaranteeRecs[i].verdict == V_VIOLATED) return false;
        return true;
    }

    /// The earliest deadline of an active obligation or prohibition
    /// (seconds * SCALE); has is false when there is none.
    function nextDeadline() external view returns (bool has, int256 due) {
        if (ended || observations == 0) return (false, 0);
        for (uint256 i = 0; i < normDefs.length; i++) {
            NormDef storage d = normDefs[i];
            NormRec storage r = recs[i];
            if (d.frozen || !d.hasDeadline || r.state != ACTIVE || (d.kind != OBLIGATION && d.kind != PROHIBITION)) continue;
            int256 el = elapsed(i, lastT);
            if (el > d.deadline) continue;
            int256 at_ = lastT + d.deadline - el;
            if (!has || at_ < due) { has = true; due = at_; }
        }
    }

    // =========================================================================
    // One observation (F.5, "Order within one observation")
    // =========================================================================

    function step(uint256 n, int256 t, Msg[] calldata msgs, bool endHere) internal {
        if (n == 0) {
            for (uint256 i = 0; i < normDefs.length; i++) {
                NormDef storage d = normDefs[i];
                if (!d.frozen && d.trigger < 0 && !d.target) setSt(i, ACTIVE, n, t);
            }
        }
        contractItems(n, t);
        Ev[] memory created = countsAs(n, t, msgs);
        powers(n, t, msgs, created);
        for (uint256 i = 0; i < normDefs.length; i++) {
            NormDef storage d = normDefs[i];
            if (d.frozen || d.trigger < 0 || !occurs(uint256(d.trigger), n, msgs, created)) continue;
            if (recs[i].state == INACTIVE) setSt(i, ACTIVE, n, t);
        }
        noteWindows(n, t);
        uint256[] memory violated = judge(n, t, msgs, created);
        for (uint256 v = 0; v < violated.length; v++) onViolation(violated[v], n, t, true);
        if (endHere) settle(n, t);
        finishItems(endHere, t);
    }

    function setSt(uint256 i, uint8 st, uint256 n, int256 t) internal {
        NormRec storage r = recs[i];
        if (r.state == ACTIVE && r.hasSince) r.before += t - r.since;
        r.state = st;
        if (st == ACTIVE) {
            r.since = t;
            r.hasSince = true;
            if (!r.hasAt) { r.hasAt = true; r.activatedAt = t; r.act = int256(n); }
        }
        if (st == VIOLATED) r.verdict = failed ? V_EXCUSED : V_VIOLATED;
        else if (st == FULFILLED || st == DISCHARGED) r.verdict = V_FULFILLED;
        if (st >= FULFILLED) {
            windows[i].head = windows[i].tail;
            deadWindows[i].head = deadWindows[i].tail;
        }
    }

    function elapsed(uint256 i, int256 t) internal view returns (int256) {
        NormRec storage r = recs[i];
        if (r.state == ACTIVE && r.hasSince) return r.before + (t - r.since);
        return r.before;
    }

    function contractItems(uint256 n, int256 t) internal {
        bool was = failed;
        failed = CadlNormsItems.step(assumeItems, assumeRecs, guaranteeItems, guaranteeRecs, data, n, was);
        if (failed && !was) failedAt = t;
    }

    function finishItems(bool endHere, int256 t) internal {
        if (endHere) ended = true;
        CadlNormsItems.finish(assumeItems, assumeRecs, guaranteeItems, guaranteeRecs, endHere);
        t;
    }

    // --- counts-as (step 1) ---------------------------------------------------------

    function countsAs(uint256 n, int256 t, Msg[] calldata msgs) internal returns (Ev[] memory created) {
        Ev[] memory buf = new Ev[](rules.length * (msgs.length + 1));
        uint256 c = 0;
        Ev[] memory none = new Ev[](0);
        for (uint256 i = 0; i < rules.length; i++) {
            Rule storage r = rules[i];
            if (r.broken) continue;
            if (r.cond >= 0) {
                Val memory cv = evalTruth(terms[uint256(r.cond)].root, n, n);
                if (!(cv.k == K_B && cv.n == 1)) continue;
            }
            Term storage w = terms[uint256(r.when)];
            if (w.kind == T_MESSAGE) {
                for (uint256 m = 0; m < msgs.length; m++) {
                    if (!msgMatches(w, msgs[m], n)) continue;
                    buf[c++] = Ev(r.creates, true, msgs[m].from, desc(msgs[m]));
                }
            } else if (occurs(uint256(r.when), n, msgs, none)) {
                buf[c++] = Ev(r.creates, false, "", w.text);
            }
        }
        created = new Ev[](c);
        for (uint256 i = 0; i < c; i++) {
            created[i] = buf[i];
            emit InstitutionalEvent(buf[i].name, buf[i].performer, buf[i].source, t);
        }
    }

    function desc(Msg calldata m) internal pure returns (string memory) {
        return string.concat(m.from, " -> ", m.to, " : ", m.name);
    }

    function occurs(uint256 j, uint256 n, Msg[] calldata msgs, Ev[] memory created) internal returns (bool) {
        Term storage term = terms[j];
        if (term.kind == T_EVENT) {
            bytes32 h = keccak256(bytes(term.text));
            for (uint256 e = 0; e < created.length; e++) if (keccak256(bytes(created[e].name)) == h) return true;
            return false;
        }
        if (term.kind == T_MESSAGE) {
            for (uint256 m = 0; m < msgs.length; m++) if (msgMatches(term, msgs[m], n)) return true;
            return false;
        }
        if (term.kind == T_PREDICATE) {
            Val memory now_ = evalTruth(term.root, n, n);
            if (!(now_.k == K_B && now_.n == 1)) return false;
            if (n == 0) return true;
            Val memory prev = evalTruth(term.root, n - 1, n - 1);
            return !(prev.k == K_B && prev.n == 1);
        }
        return false;
    }

    // --- powers (step 2) -------------------------------------------------------------

    function powers(uint256 n, int256 t, Msg[] calldata msgs, Ev[] memory created) internal {
        Act[] memory acts = collectActs(n, msgs, created);
        bool[] memory active = new bool[](normDefs.length);
        for (uint256 p = 0; p < normDefs.length; p++) active[p] = recs[p].state == ACTIVE;
        for (uint256 a = 0; a < acts.length; a++) exercise(acts[a], active, n, t);
    }

    function eventUsedByPower(string memory name) internal view returns (bool) {
        bytes32 h = keccak256(bytes(name));
        for (uint256 p = 0; p < normDefs.length; p++) {
            NormDef storage d = normDefs[p];
            if (d.kind == POWER && !d.frozen && terms[uint256(d.content)].kind == T_EVENT
                && keccak256(bytes(terms[uint256(d.content)].text)) == h) return true;
        }
        return false;
    }

    function collectActs(uint256 n, Msg[] calldata msgs, Ev[] memory created) internal returns (Act[] memory acts) {
        Act[] memory buf = new Act[](created.length + normDefs.length * msgs.length);
        uint256 c = 0;
        for (uint256 e = 0; e < created.length; e++) {
            if (created[e].hasPerformer && eventUsedByPower(created[e].name)) {
                buf[c++] = Act(created[e].name, created[e].performer,
                               string.concat("institutional event ", created[e].name));
            }
        }
        uint256 firstMsgAct = c;
        for (uint256 p = 0; p < normDefs.length; p++) {
            NormDef storage d = normDefs[p];
            if (d.kind != POWER || d.frozen || terms[uint256(d.content)].kind != T_MESSAGE) continue;
            for (uint256 m = 0; m < msgs.length; m++) {
                if (!msgMatches(terms[uint256(d.content)], msgs[m], n)) continue;
                Act memory act = Act(terms[uint256(d.content)].text, msgs[m].from,
                                     string.concat("message ", desc(msgs[m])));
                if (!seenAct(buf, firstMsgAct, c, act)) buf[c++] = act;
            }
        }
        acts = new Act[](c);
        for (uint256 i = 0; i < c; i++) acts[i] = buf[i];
    }

    function seenAct(Act[] memory buf, uint256 from, uint256 to, Act memory act) internal pure returns (bool) {
        for (uint256 q = from; q < to; q++) {
            if (keccak256(bytes(buf[q].content)) == keccak256(bytes(act.content))
                && keccak256(bytes(buf[q].desc)) == keccak256(bytes(act.desc))) return true;
        }
        return false;
    }

    function exercise(Act memory act, bool[] memory active, uint256 n, int256 t) internal {
        bool any = false;
        bytes32 h = keccak256(bytes(act.content));
        for (uint256 p = 0; p < normDefs.length; p++) {
            NormDef storage d = normDefs[p];
            if (d.kind != POWER || d.frozen || !active[p]) continue;
            if (keccak256(bytes(terms[uint256(d.content)].text)) != h) continue;
            if (!isBearer(p, act.performer)) continue;
            any = true;
            emit PowerExercised(d.id, act.performer, act.desc, t);
            applyEffects(d.activate, INACTIVE, INACTIVE, ACTIVE, n, t);
            applyEffects(d.suspend, ACTIVE, ACTIVE, SUSPENDED, n, t);
            applyEffects(d.resume, SUSPENDED, SUSPENDED, ACTIVE, n, t);
            applyEffects(d.discharge, ACTIVE, SUSPENDED, DISCHARGED, n, t);
        }
        if (!any) emit Unauthorised(act.desc, act.performer, t);
    }

    function applyEffects(uint256[] storage targets, uint8 from1, uint8 from2, uint8 to, uint256 n, int256 t) internal {
        for (uint256 k = 0; k < targets.length; k++) {
            uint256 x = targets[k];
            if (normDefs[x].frozen) continue;
            uint8 s = recs[x].state;
            if (s == from1 || s == from2) setSt(x, to, n, t);
        }
    }

    function isBearer(uint256 p, string memory performer) internal view returns (bool) {
        NormDef storage d = normDefs[p];
        if (!d.hasBearer) return false;
        (string memory text, bool bound) = actorText(d.bearer.name, d.bearer.ik, d.bearer.idx);
        if (bound) return keccak256(bytes(performer)) == keccak256(bytes(text));
        (string memory name, bool hasIdx, string memory idx) = CadlNormsValues.splitActor(performer);
        return keccak256(bytes(name)) == keccak256(bytes(d.bearer.name)) && isMember(name, hasIdx, idx);
    }

    // --- contents, deadlines and until (step 4) -----------------------------------------

    function judge(uint256 n, int256 t, Msg[] calldata msgs, Ev[] memory created) internal returns (uint256[] memory violated) {
        uint256[] memory buf = new uint256[](normDefs.length);
        uint256 c = 0;
        for (uint256 i = 0; i < normDefs.length; i++) {
            NormDef storage d = normDefs[i];
            if (d.frozen || n < recs[i].from) continue;
            if (recs[i].state == ACTIVE && (d.kind == OBLIGATION || d.kind == PROHIBITION)) {
                if (d.tmode != 0 ? judgeWindow(i, n, t) : judgeContent(i, n, t, msgs, created)) buf[c++] = i;
            }
            uint8 st = recs[i].state;
            if ((st == ACTIVE || st == SUSPENDED) && d.until >= 0 && occurs(uint256(d.until), n, msgs, created)) {
                setSt(i, DISCHARGED, n, t);
            }
        }
        violated = new uint256[](c);
        for (uint256 i = 0; i < c; i++) violated[i] = buf[i];
    }

    /// true when the norm is violated. The content has no temporal
    /// operator, so it is decided at this observation.
    function judgeContent(uint256 i, uint256 n, int256 t, Msg[] calldata msgs, Ev[] memory created) internal returns (bool) {
        NormDef storage d = normDefs[i];
        bool open = !d.hasDeadline || elapsed(i, t) <= d.deadline;
        Val memory v = open ? contentValue(i, n, n, msgs, created) : boolVal(false);
        return decide(i, n, t, v.k == K_B && v.n == 1, !open);
    }

    /// A hit fulfils an obligation and violates a prohibition; a closed
    /// window without one does the opposite. true when violated.
    function decide(uint256 i, uint256 n, int256 t, bool hit, bool closed) internal returns (bool) {
        bool obligation = normDefs[i].kind == OBLIGATION;
        if (hit || closed) setSt(i, hit == obligation ? FULFILLED : VIOLATED, n, t);
        return (hit || closed) && hit != obligation;
    }

    /// A content that is one temporal operator (see CadlNormsWindows).
    function judgeWindow(uint256 i, uint256 n, int256 t) internal returns (bool) {
        NormDef storage d = normDefs[i];
        bool open = !d.hasDeadline || elapsed(i, t) <= d.deadline;
        (bool hit, bool empty) = CadlNormsWindows.judge(windows[i], deadWindows[i], data, temporals[i],
                                                        n, t, open);
        return decide(i, n, t, hit, !hit && !open && empty);
    }

    function noteWindows(uint256 n, int256 t) internal {
        for (uint256 i = 0; i < normDefs.length; i++) {
            NormDef storage d = normDefs[i];
            if (d.tmode == 0 || d.frozen) continue;
            CadlNormsWindows.note(windows[i], deadWindows[i], data, temporals[i], n, t);
        }
    }

    function contentValue(uint256 i, uint256 k, uint256 n, Msg[] calldata msgs, Ev[] memory created) internal returns (Val memory) {
        Term storage term = terms[uint256(normDefs[i].content)];
        if (term.kind == T_PREDICATE) {
            Val memory v = evalTruth(term.root, k, n);
            if (v.k == K_U) return boolVal(false);
            return v;
        }
        if (k != n) return boolVal(false);
        return boolVal(occurs(uint256(normDefs[i].content), n, msgs, created));
    }

    // --- violations (step 5) and the end (step 6) ------------------------------------------

    function onViolation(uint256 i, uint256 n, int256 t, bool activate) internal {
        NormDef storage d = normDefs[i];
        bool repActivated = false;
        if (activate && d.reparation >= 0) {
            uint256 rp = uint256(d.reparation);
            if (!normDefs[rp].frozen && recs[rp].state == INACTIVE) repActivated = true;
        }
        string memory transition = "";
        bool terminal = data.hasLifecycle && lcTerminal[keccak256(bytes(data.obsSt[n]))];
        if (d.hasTransition && activate && !terminal) transition = d.transition;
        emit Violation(d.id, d.severity, recs[i].verdict, d.sanction, d.reparationId, repActivated, transition, t);
        if (repActivated) {
            uint256 rp = uint256(d.reparation);
            recs[rp].from = n + 1;
            setSt(rp, ACTIVE, n, t);
        }
    }

    function settle(uint256 n, int256 t) internal {
        ended = true;
        uint256[] memory late = new uint256[](normDefs.length);
        uint256 c = 0;
        for (uint256 i = 0; i < normDefs.length; i++) {
            NormDef storage d = normDefs[i];
            NormRec storage r = recs[i];
            if (d.frozen || r.state >= FULFILLED) continue;
            bool inForce = r.state == ACTIVE || r.state == SUSPENDED;
            if (inForce && r.act == int256(n)) { setSt(i, DISCHARGED, n, t); continue; }
            if (inForce && (d.kind == OBLIGATION || d.kind == PROHIBITION)
                && (windows[i].head < windows[i].tail || deadWindows[i].head < deadWindows[i].tail)) continue;
            if (inForce && d.kind == OBLIGATION) {
                if (!d.hasDeadline) { setSt(i, VIOLATED, n, t); late[c++] = i; }
                else if (elapsed(i, t) <= d.deadline) setSt(i, DISCHARGED, n, t);
                else { setSt(i, VIOLATED, n, t); late[c++] = i; }
            } else if (inForce && d.kind == PROHIBITION) {
                setSt(i, FULFILLED, n, t);
            } else {
                setSt(i, DISCHARGED, n, t);
            }
        }
        for (uint256 i = 0; i < c; i++) onViolation(late[i], n, t, false);
    }

    // --- marks and changes ---------------------------------------------------------------

    function itemMark(ItemRec storage r, bool guarantee) internal view returns (uint8) {
        if (guarantee) return r.verdict;
        return r.value == 1 ? 1 : r.value == 2 ? 2 : 0;
    }

    function marks() internal view returns (uint8[] memory ns, uint8[] memory nv, uint8[] memory am, uint8[] memory gm) {
        ns = new uint8[](recs.length);
        nv = new uint8[](recs.length);
        for (uint256 i = 0; i < recs.length; i++) { ns[i] = recs[i].state; nv[i] = recs[i].verdict; }
        am = new uint8[](assumeRecs.length);
        for (uint256 i = 0; i < assumeRecs.length; i++) am[i] = itemMark(assumeRecs[i], false);
        gm = new uint8[](guaranteeRecs.length);
        for (uint256 i = 0; i < guaranteeRecs.length; i++) gm[i] = itemMark(guaranteeRecs[i], true);
    }

    function emitChanges(uint8[] memory ns, uint8[] memory nv, uint8[] memory am, uint8[] memory gm, int256 t) internal {
        for (uint256 i = 0; i < recs.length; i++) {
            if (recs[i].state != ns[i] || recs[i].verdict != nv[i]) {
                emit NormChanged(i, normDefs[i].id, recs[i].state, recs[i].verdict, ns[i], nv[i], t);
            }
        }
        for (uint256 i = 0; i < assumeRecs.length; i++) {
            uint8 m = itemMark(assumeRecs[i], false);
            if (m != am[i]) emit ItemChanged(false, i + 1, m, am[i], t);
        }
        for (uint256 i = 0; i < guaranteeRecs.length; i++) {
            uint8 m = itemMark(guaranteeRecs[i], true);
            if (m != gm[i]) emit ItemChanged(true, i + 1, m, gm[i], t);
        }
    }

    // =========================================================================
    // Messages and actors
    // =========================================================================

    function msgMatches(Term storage term, Msg calldata m, uint256 n) internal returns (bool) {
        if (keccak256(bytes(m.name)) != keccak256(bytes(term.msgName))) return false;
        if (!actorMatches(term.sender, m.from) || !actorMatches(term.receiver, m.to)) return false;
        if (!term.hasArgs) return true;
        if (term.args.length != m.args.length) return false;
        for (uint256 a = 0; a < term.args.length; a++) {
            Val memory w = evalAt(term.args[a], n, n);
            if (!eqRaw(w, m.args[a])) return false;
        }
        return true;
    }

    function eqRaw(Val memory w, ValIn calldata a) internal pure returns (bool) {
        if (w.k == K_U || w.k == K_D || a.k == K_U) return false;
        if ((w.k == K_B) != (a.k == K_B)) return false;
        if (w.k == K_T || w.k == K_S) return a.k == K_T && keccak256(bytes(w.s)) == keccak256(bytes(a.s));
        if (a.k == K_T) return false;
        return w.n == a.n;
    }

    function actorMatches(Pat storage pat, string calldata actual) internal view returns (bool) {
        (string memory name, bool hasIdx, string memory idx) = CadlNormsValues.splitActor(actual);
        if (keccak256(bytes(name)) != keccak256(bytes(pat.name)) || !isMember(name, hasIdx, idx)) return false;
        if (pat.ik == I_ANY) {
            if (!hasIdx) return false;
            if (!pat.hasRange) return true;
            int256 v = CadlNormsValues.parseIndex(idx);
            return v >= pat.lo && v <= pat.hi;
        }
        (string memory text, bool bound) = actorText(pat.name, pat.ik, pat.idx);
        if (bound) return keccak256(bytes(actual)) == keccak256(bytes(text));
        return true;
    }

    function isMember(string memory name, bool hasIdx, string memory idx) internal view returns (bool) {
        ActorDecl storage a = actors[keccak256(bytes(name))];
        if (!a.declared) return false;
        if (!a.isIndexed || !hasIdx) return !a.isIndexed && !hasIdx;
        if (!CadlNormsValues.isDigits(idx)) return false;
        int256 v = CadlNormsValues.parseIndex(idx);
        return (!a.hasLo || a.lo <= v) && (!a.hasHi || v <= a.hi);
    }

    // --- checks of the input ---------------------------------------------------------------

    // =========================================================================
    // Values (A.10 with the three-valued reading of F.5)
    // =========================================================================

    function evalAt(uint256 root, uint256 p, uint256 n) internal returns (Val memory) {
        return CadlNormsEval.evaluate(data, root, p, n);
    }

    function evalTruth(uint256 root, uint256 p, uint256 n) internal returns (Val memory) {
        return truth(CadlNormsEval.evaluate(data, root, p, n));
    }

    function truth(Val memory v) internal pure returns (Val memory) {
        if (v.k == K_U || v.k == K_D || v.k == K_B) return v;
        if (v.k == K_N) return boolVal(v.n != 0);
        return mark(K_U);
    }


    /// The text of an actor reference with its index bound; bound is false
    /// for an index name that the instance does not bind.
    function actorText(string memory name, uint8 ik, string memory idx) internal view returns (string memory, bool) {
        if (ik == I_NONE) return (name, true);
        if (ik == I_LIT) return (string.concat(name, "[", idx, "]"), true);
        bytes32 h = keccak256(bytes(idx));
        if (data.hasBinding[h]) {
            Stored storage b = data.bindings[h];
            string memory text = b.k == K_T ? b.s : CadlNormsValues.intText(b.n / SCALE);
            return (string.concat(name, "[", text, "]"), true);
        }
        return (string.concat(name, "[", idx, "]"), false);
    }


    function boolVal(bool b) internal pure returns (Val memory) {
        return Val(K_B, b ? int256(1) : int256(0), "");
    }

    function mark(uint8 k) internal pure returns (Val memory) {
        return Val(k, 0, "");
    }

    function isText(Val memory v) internal pure returns (bool) {
        return v.k == K_T || v.k == K_S;
    }

    function eqVal(Val memory a, Val memory b) internal pure returns (bool) {
        if ((a.k == K_B) != (b.k == K_B)) return false;
        if (isText(a) || isText(b)) return isText(a) && isText(b) && keccak256(bytes(a.s)) == keccak256(bytes(b.s));
        return a.n == b.n;
    }

    // =========================================================================
    // Filling the model (used by the constructor of the generated contract)
    // =========================================================================

    function _node(uint8 op, uint8 bop, uint256 a, uint256 b, int256 d, uint8 ik,
                   string memory s1, string memory s2, string memory s3, uint8 lk, int256 ln, string memory ls) internal {
        Node storage nd = data.nodes.push();
        nd.op = op; nd.bop = bop; nd.a = a; nd.b = b; nd.d = d; nd.ik = ik;
        nd.s1 = s1; nd.s2 = s2; nd.s3 = s3;
        nd.lit = Val(lk, ln, ls);
    }

    function _item(uint256 i) internal {
        data.nodes[data.nodes.length - 1].items.push(i);
    }

    function _pat(Pat storage p, string memory name, uint8 ik, string memory idx, bool hasRange, int256 lo, int256 hi) internal {
        p.name = name; p.ik = ik; p.idx = idx; p.hasRange = hasRange; p.lo = lo; p.hi = hi;
    }

    function _term(uint8 kind, string memory text, uint256 root, string memory msgName, bool hasArgs) internal returns (Term storage t) {
        t = terms.push();
        t.kind = kind; t.text = text; t.root = root; t.msgName = msgName; t.hasArgs = hasArgs;
    }

    function _norm(string memory id, uint8 kind, bool frozen, int256 trigger, int256 until, int256 content,
                   bool hasDeadline, int256 deadline, bool target) internal returns (NormDef storage d) {
        d = normDefs.push();
        d.id = id; d.kind = kind; d.frozen = frozen; d.trigger = trigger; d.until = until; d.content = content;
        d.hasDeadline = hasDeadline; d.deadline = deadline; d.target = target;
        d.reparation = -1;
        NormRec storage r = recs.push();
        r.act = -1;
    }

    function _temporal(uint8 mode, bool neg, uint256 tp, uint256 tq, int256 td, uint256 node,
                       uint256 root) internal {
        uint256 i = normDefs.length - 1;
        normDefs[i].tmode = mode;
        temporals[i] = Temporal(mode, neg, tp, tq, td, node, root);
    }

    function _actor(string memory name, bool isIndexed_, bool hasLo, int256 lo, bool hasHi, int256 hi) internal {
        actors[keccak256(bytes(name))] = ActorDecl(true, isIndexed_, hasLo, lo, hasHi, hi);
    }

    function _assume(string memory text, bool temporal_, bool broken, bool unsupported, uint256 root) internal {
        assumeItems.push(Item(text, temporal_, broken, unsupported, root));
        assumeRecs.push(ItemRec(0, false, V_PENDING));
    }

    function _guarantee(string memory text, bool temporal_, bool broken, bool unsupported, uint256 root) internal {
        guaranteeItems.push(Item(text, temporal_, broken, unsupported, root));
        guaranteeRecs.push(ItemRec(0, false, V_PENDING));
    }
}
// <<< norms-monitor-base
