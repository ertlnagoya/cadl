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
// a truth value as a code: undecided, true, false, undefined
uint8 constant X_D = 0;
uint8 constant X_T = 1;
uint8 constant X_F = 2;
uint8 constant X_U = 3;
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

    /// The truth of `root` at observation n for every combination of the
    /// values of the temporal operators ops (at most four): entry e reads
    /// operator i as the digit i of e in base 4 (X_D, X_T, X_F, X_U).
    /// Returns the entries at which the content is true and those at which
    /// it is undecided (bit e). The rest of the content is read once.
    function table(EvalData storage data, uint256 root, uint256 n, uint256[] memory ops)
            public returns (uint256 ts, uint256 ds) {
        Vt memory r = vt(data, root, n, ops);
        if (r.x) return (r.c[X_T], r.c[X_D]);
        uint8 c = code(truth(r.s));
        return (c == X_T ? type(uint256).max : 0, c == X_D ? type(uint256).max : 0);
    }

    /// A node as one value s (no operator below it), or (x) as the entries
    /// c[v] at which its truth value is v (a value with an operator below
    /// it is a truth value).
    struct Vt { bool x; Val s; uint256[4] c; }

    function vt(EvalData storage data, uint256 id, uint256 n, uint256[] memory ops) internal returns (Vt memory r) {
        for (uint256 i = 0; i < ops.length; i++) {
            if (id != ops[i]) continue;
            r.x = true;
            for (uint256 c = 0; c < 4; c++) r.c[c] = digits(i, c);
            return r;
        }
        Node storage nd = data.nodes[id];
        uint8 op = nd.op;
        if (op == OP_NOT || op == OP_AND || op == OP_OR || op == OP_BIN) {
            Vt memory l = vt(data, nd.a, n, ops);
            Vt memory q;
            if (op != OP_NOT) q = vt(data, nd.b, n, ops);
            if (!l.x && !q.x) { r.s = combine(op, nd.bop, l.s, q.s); return r; }
            r.x = true;
            r.c = entries(op, nd.bop, l, q);
            return r;
        }
        if (op == OP_IN) return inVec(data, nd, n, ops);
        r.s = ev(data, id, n, n);
    }

    /// The entries whose digit i (base 4) is c.
    function digits(uint256 i, uint256 c) internal pure returns (uint256 v) {
        uint256 b = 1 << (2 * i);                      // 4^i
        v = ((1 << b) - 1) << (c * b);
        for (uint256 s = 4 * b; s < 256; s *= 2) v |= v << s;
    }

    /// The node op of l and r: the operands take one value each (s) or
    /// four classes of entries, so at most 16 combinations are read.
    function entries(uint8 op, uint8 bop, Vt memory l, Vt memory r) internal pure returns (uint256[4] memory out) {
        for (uint256 cl = 0; cl < (l.x ? 4 : 1); cl++) {
            uint256 ml = l.x ? l.c[cl] : type(uint256).max;
            if (ml == 0) continue;
            for (uint256 cr = 0; cr < (r.x ? 4 : 1); cr++) {
                uint256 both = ml & (r.x ? r.c[cr] : type(uint256).max);
                if (both == 0) continue;
                out[code(truth(combine(op, bop, l.x ? codeVal(cl) : l.s, r.x ? codeVal(cr) : r.s)))] |= both;
            }
        }
    }

    function combine(uint8 op, uint8 bop, Val memory l, Val memory r) internal pure returns (Val memory) {
        if (op == OP_NOT) {
            Val memory v = truth(l);
            return v.k == K_B ? boolVal(v.n == 0) : v;
        }
        if (op == OP_BIN) return CadlNormsValues.binop(bop, l, r);
        return andOr(op == OP_OR, truth(l), truth(r));
    }

    function inVec(EvalData storage data, Node storage nd, uint256 n, uint256[] memory ops)
            internal returns (Vt memory out) {
        uint256 m = nd.items.length;
        Vt[] memory xs = new Vt[](m + 1);
        bool any = false;
        for (uint256 j = 0; j <= m; j++) {
            xs[j] = vt(data, j == 0 ? nd.a : nd.items[j - 1], n, ops);
            any = any || xs[j].x;
        }
        uint256 count = any ? 1 << (2 * ops.length) : 1;
        for (uint256 e = 0; e < count; e++) {
            Val[] memory items = new Val[](m);
            for (uint256 j = 1; j <= m; j++) items[j - 1] = xs[j].x ? codeVal(classOf(xs[j], e)) : xs[j].s;
            Val memory r = inVals(xs[0].x ? codeVal(classOf(xs[0], e)) : xs[0].s, items);
            if (!any) { out.s = r; return out; }
            out.c[code(truth(r))] |= 1 << e;
        }
        out.x = true;
    }

    function classOf(Vt memory v, uint256 e) internal pure returns (uint256) {
        for (uint256 c = 0; c < 3; c++) if (v.c[c] >> e & 1 != 0) return c;
        return 3;
    }

    function code(Val memory v) internal pure returns (uint8) {
        if (v.k == K_D) return X_D;
        if (v.k == K_B) return v.n == 1 ? X_T : X_F;
        return X_U;
    }

    function codeVal(uint256 c) internal pure returns (Val memory) {
        if (c == X_D) return mark(K_D);
        if (c == X_U) return mark(K_U);
        return boolVal(c == X_T);
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
        Val[] memory items = new Val[](nd.items.length);
        for (uint256 i = 0; i < nd.items.length; i++) items[i] = ev(data, nd.items[i], p, n);
        return inVals(l, items);
    }

    function inVals(Val memory l, Val[] memory items) internal pure returns (Val memory) {
        if (l.k == K_U || l.k == K_D) return l;
        if (l.k == K_S) return mark(K_U);
        bool sawD = false;
        bool sawU = false;
        for (uint256 i = 0; i < items.length; i++) {
            Val memory it = items[i];
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


// The candidates of a norm whose content has one temporal operator (F.4,
// F.5): the observations at which the content has to be decided, oldest
// first, with what the observations since the last judgement say about
// the operands. Each candidate is decided from these summaries; no
// window is scanned per candidate, so the cost of an observation does
// not grow with the number of candidates.
struct Window {
    uint256 head; uint256 tail; uint256 purge;     // candidates [head, tail); entries before purge are deleted
    bool luSet; uint256 lu;                        // candidates at or before lu have an operand that was unknown
                                                   // (release: a p that was not true)
    bool luqSet; uint256 luq;                      // release: candidates at or before luq have seen q not false
    bool ffSet; int256 ff; uint256 ffi;            // throughout: the first time false; release: the first p false
    bool fuSet; int256 fu;                         // throughout: the first time unknown
    bool ftSet; int256 ft;                         // within: the first time true
    bool jSet; uint256 j; int256 jt;               // until, release: the first observation with q true
    bool pSet; uint256 pFirst; bool pFalse; bool pUnknown;   // until, release: p not true
    bool qnSet; uint256 qn; int256 qnt;            // release: the first observation with q not false
    uint256 at; uint8 pv; uint8 qv;                // the operands at observation at - 1
    mapping(uint256 => uint256) k;                 // the observation of a candidate
    mapping(uint256 => int256) t;                  // its time
}

// The three queues of a norm: the candidates that are true exactly when
// the operator is true, those that are true exactly when it is false (the
// dual operator on the negated operands: NOT within(d, p) is throughout(d,
// NOT p), NOT throughout(d, p) is within(d, NOT p), NOT until(p, q, d) is
// the release of D.5), and those that are never true but undecided until
// the operator is decided.
struct Windows { Window live; Window dual; Window dead; }

uint8 constant W_WITHIN = 1;
uint8 constant W_THROUGHOUT = 2;
uint8 constant W_UNTIL = 3;
uint8 constant W_RELEASE = 4;

// The temporal operator of the content of a norm: its kind, its operands
// (p, and q of until), its duration, the operator node and the root of the
// content.
struct Temporal { uint8 mode; uint256 tp; uint256 tq; int256 td; uint256 node; uint256 root; }

/// The judgement of the candidates of a temporal content, with a cost per
/// observation that does not depend on the number of candidates: a few
/// binary searches over them, a few deletions of decided ones.
///
/// The rest of the content is decided at the observation of a candidate
/// (its names are fixed, see makeKnown), so a candidate is one of: true
/// whatever the operator gives (a hit), never true (not kept), true
/// exactly when the operator is true (`live`), true exactly when it is
/// false (`dual`), true when it is true or false (in both), or never true
/// but undecided until the operator is decided (`dead`: it keeps the norm
/// pending, as F.5 does).
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
    uint8 internal constant C_DUAL = 4;
    uint8 internal constant C_BOTH = 5;

    /// What an observation says about the operands, for the candidates
    /// there are (also while the norm is suspended).
    function note(Windows storage ws, EvalData storage data, Temporal storage tm, uint256 n, int256 t) public {
        if (ws.live.head == ws.live.tail && ws.dual.head == ws.dual.tail && ws.dead.head == ws.dead.tail) return;
        (uint8 pv, uint8 qv) = operands(ws.live, data, tm, n);
        uint8 mode = tm.mode;
        summarize(ws.live, mode, n, t, pv, qv);
        summarize(ws.dual, dualMode(mode), n, t, dualP(mode, pv), qv);
        summarize(ws.dead, mode, n, t, pv, qv);
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
            if (mode == W_RELEASE) {
                if (qv != F && !w.qnSet) { w.qnSet = true; w.qn = n; w.qnt = t; }
                if (pv == F && !w.ffSet) { w.ffSet = true; w.ff = t; w.ffi = n; }
            }
        }
    }

    /// The judgement of observation n (F.5, items 2 and 3) when the norm
    /// is active: whether a candidate holds (hit), and whether none is left
    /// (empty). `open`: the deadline has not passed, so n is a candidate.
    function judge(Windows storage ws, EvalData storage data, Temporal storage tm, uint256 n, int256 t, bool open)
            public returns (bool hit, bool empty) {
        (uint8 pv, uint8 qv) = operands(ws.live, data, tm, n);
        uint8 mode = tm.mode;
        uint8 cls = open ? classify(data, tm, n) : C_DROP;
        bool toLive = cls == C_LIVE || cls == C_BOTH;
        bool toDual = cls == C_DUAL || cls == C_BOTH;
        hit = cls == C_HIT || (toLive && trueNow(mode, pv, qv))
            || (toDual && trueNow(dualMode(mode), dualP(mode, pv), qv))
            || (ws.live.head < ws.live.tail && oldHit(ws.live, mode, tm.td, t))
            || (ws.dual.head < ws.dual.tail && oldHit(ws.dual, dualMode(mode), tm.td, t));
        if (hit) {
            clear(ws);
        } else {
            advance(ws.live, mode, tm.td, n, t, pv, qv, toLive);
            advance(ws.dual, dualMode(mode), tm.td, n, t, dualP(mode, pv), qv, toDual);
            advance(ws.dead, mode, tm.td, n, t, pv, qv, cls == C_DEAD && !trueNow(mode, pv, qv));
        }
        reset(ws.live);
        reset(ws.dual);
        reset(ws.dead);
        empty = ws.live.head == ws.live.tail && ws.dual.head == ws.dual.tail && ws.dead.head == ws.dead.tail;
    }

    function clear(Windows storage ws) internal {
        ws.live.head = ws.live.tail;
        ws.dual.head = ws.dual.tail;
        ws.dead.head = ws.dead.tail;
    }

    function pending(Windows storage ws) internal view returns (bool) {
        return ws.live.head < ws.live.tail || ws.dual.head < ws.dual.tail || ws.dead.head < ws.dead.tail;
    }

    function dualMode(uint8 mode) internal pure returns (uint8) {
        return mode == W_WITHIN ? W_THROUGHOUT : mode == W_THROUGHOUT ? W_WITHIN : W_RELEASE;
    }

    /// The operand p of the dual operator: NOT p for within and throughout;
    /// the release reads p and q as they are.
    function dualP(uint8 mode, uint8 pv) internal pure returns (uint8) {
        if (mode == W_UNTIL || pv == U) return pv;
        return pv == T ? F : T;
    }

    /// The operator is true at n for a candidate of n itself.
    function trueNow(uint8 mode, uint8 pv, uint8 qv) internal pure returns (bool) {
        if (mode == W_WITHIN) return pv == T;
        if (mode == W_UNTIL) return qv == T;
        if (mode == W_RELEASE) return qv == F && pv == F;
        return false;
    }

    /// The candidates after an observation without a hit: false in every
    /// window (throughout: an operand false; until: p false; release: p
    /// false) removes them all, an unknown operand marks them, the windows
    /// that are over go; then the new candidate (unless the operator is
    /// decided at it).
    function advance(Window storage w, uint8 mode, int256 d, uint256 n, int256 t, uint8 pv, uint8 qv, bool add)
            internal {
        bool until = mode == W_UNTIL || mode == W_RELEASE;
        if (until && w.head < w.tail && w.jSet && (!w.pSet || w.j <= w.pFirst)) {
            // until is true for the candidates with p true before j whose
            // window has j: a candidate here is then decided (dead: never
            // true; release: false), so they go
            uint256 from = firstFrom(w, w.jt - d);
            uint256 firstClean = firstAfterK(w, w.luSet, w.lu);
            uint256 cut = from > firstClean ? from : firstClean;
            if (cut < w.tail) w.tail = cut;
        }
        if (w.head < w.tail) {
            if ((mode == W_THROUGHOUT && w.ffSet) || (until && w.pFalse) || (mode == W_WITHIN && w.ftSet)) {
                w.head = w.tail;
            } else {
                if ((mode == W_THROUGHOUT && w.fuSet) || (until && w.pUnknown)) {
                    w.luSet = true;
                    w.lu = w.k[w.tail - 1];
                }
                if (mode == W_RELEASE && w.qnSet) {
                    w.luqSet = true;
                    w.luq = w.k[w.tail - 1];
                }
                w.head = firstFrom(w, t - d);
            }
        }
        if (!add || (mode != W_WITHIN && pv == F) || (mode == W_RELEASE && qv == T)) return;
        if (mode != W_WITHIN && pv == U) { w.luSet = true; w.lu = n; }
        if (mode == W_RELEASE && qv == U) { w.luqSet = true; w.luq = n; }
        w.k[w.tail] = n;
        w.t[w.tail] = t;
        w.tail++;
    }

    function reset(Window storage w) internal {
        w.ffSet = false; w.fuSet = false; w.ftSet = false; w.qnSet = false;
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
        uint256[] memory ops = new uint256[](1);
        ops[0] = tm.node;
        (uint256 ts, uint256 ds) = CadlNormsEval.table(data, tm.root, n, ops);
        if (ts & 1 != 0) return C_HIT;
        if (ds & 1 == 0) return C_DROP;
        bool l = ts >> X_T & 1 != 0;
        bool f = ts >> X_F & 1 != 0;
        return l ? (f ? C_BOTH : C_LIVE) : (f ? C_DUAL : C_DEAD);
    }

    /// The operands at observation n (0 false, 1 true, 2 unknown), once per
    /// observation.
    function operands(Window storage w, EvalData storage data, Temporal storage tm, uint256 n)
            internal returns (uint8 pv, uint8 qv) {
        if (w.at == n + 1) return (w.pv, w.qv);
        pv = code(CadlNormsEval.evaluate(data, tm.tp, n, n));
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
        if (mode == W_RELEASE) {
            // p false at ffi inside the window of the newest candidate, with
            // q false before it and at it
            if (w.ffSet && w.t[last] + d >= w.ff && (!w.luqSet || w.k[last] > w.luq)
                && (!w.qnSet || w.qn > w.ffi)) return true;
            // or a window that is over before p was false, with q false in it
            int256 lim = t;
            if (w.ffSet && w.ff < lim) lim = w.ff;
            if (w.qnSet && w.qnt < lim) lim = w.qnt;
            uint256 i = firstAfterK(w, w.luqSet, w.luq);
            return i < w.tail && w.t[i] + d < lim;
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

// Contents with two or more temporal operators, or with a temporal operator
// inside another one (F.4, F.5). Each operator has its own record of what
// the observations say about its operands, and the candidates of the norm
// are the leaves of a segment tree in storage. A leaf holds, for its
// candidate, the values of the content for every combination of values
// that its undecided operators can still take: the rest of the content is
// decided at the observation of the candidate, so these values are
// computed there once (CadlNormsEval.table). An operator is decided for a
// range of candidates at a time (a window that is over, an operand that
// decides every open window), so an observation changes a few ranges of
// the tree, each in a number of steps that grows with the logarithm of the
// number of candidates; the root then says whether some candidate holds
// and whether some candidate is still undecided.
//
// A node gives operator i a state s_i: 0 when the operator is decided for
// the candidate (its value is then part of the value of the content), or
// 1 + f when it is undecided and takes the value f (X_D, X_T, X_F or X_U)
// in the future considered. The states P = sum of s_i 5^i have two bits
// each: bit 2 (P mod 125) of word P / 125 says that a candidate below is
// true in that future, the next bit that one is undecided in it. A node
// is one word for up to three operators and 5^(m - 3) words for m > 3.
// Bits 250 + 2 (i mod 3) of word i / 3 hold the tag of the lazy
// propagation of operator i: the value (X_T, X_F or X_U) to which it is
// decided for the undecided candidates below, or 0.
//
// A nested operator (W_NESTED) is an operator `outer` (within or
// throughout, duration td) whose operand is throughout(id, p) or its
// negation; within(id, p) is NOT throughout(id, NOT p). The inner value
// at an observation becomes known later; the observations whose inner
// value is decided are always the first ones, so these values are fed to
// the outer operator in the order of the observations (D.5).
struct MOp {
    uint8 mode; uint256 tp; uint256 tq; int256 td; uint256 node;
    bool uSet; uint256 u;        // the candidates at or before u have seen an undefined operand
    bool puSet; uint256 pu;      // until: the last observation with p undefined
    uint8 outer; bool ineg; bool oneg; int256 id;    // W_NESTED: p is tp, negated when ineg;
                                                     // the inner value is negated when oneg
    uint256 fed;                 // W_NESTED: the inner value is decided before observation fed
    bool iuSet; uint256 iu;      // W_NESTED: the last observation with p undefined
}

uint8 constant W_NESTED = 5;

struct Multi {
    uint256 root;                // the root of the content
    MOp[] ops;
    uint256 words;               // the storage words of a node
    uint256 gen;                 // the tree of the current activation
    uint256 tail;                // the number of candidates
    uint256 h;                   // the tree covers the candidates [0, 2^h)
    bool pend;                   // a candidate was undecided at the last judgement
    uint256 noted;               // n + 1 once observation n has been applied
    uint256 cur;                 // the operators for a candidate at that observation, 2 bits each
    mapping(uint256 => uint256) k;       // the observation of a candidate
    mapping(uint256 => int256) t;        // its time
    mapping(uint256 => uint256) nodes;
}

library CadlNormsMulti {
    uint256 internal constant BITS = (1 << 250) - 1;
    // the states with every operator decided or undecided (X_D) in the future
    uint256 internal constant CUR_T = 0x5014000000001405;
    uint256 internal constant CUR_D = 0xa02800000000280a;
    // the states with operator 0, 1 or 2 decided
    uint256 internal constant KEEP0 = 0x300c0300c0300c0300c0300c0300c0300c0300c0300c0300c0300c0300c03;
    uint256 internal constant KEEP1 = 0x3ff0000000000ffc0000000003ff0000000000ffc0000000003ff;
    uint256 internal constant KEEP2 = 0x3ffffffffffff;

    /// An observation, for the candidates there are (also while the norm
    /// is suspended).
    function note(Multi storage mu, EvalData storage data, uint256 n, int256 t) public {
        if (mu.tail > 0) observe(mu, data, n, t);
    }

    /// The judgement of observation n when the norm is active: whether a
    /// candidate holds, and whether none is undecided. `open`: the deadline
    /// has not passed, so n is a candidate.
    function judge(Multi storage mu, EvalData storage data, uint256 n, int256 t, bool open)
            public returns (bool hit, bool empty) {
        if (mu.noted != n + 1) observe(mu, data, n, t);
        if (open) add(mu, data, n, t);
        bool und = false;
        if (mu.tail > 0) {
            uint256[] memory x = load(mu, key(mu, mu.h, 0));
            for (uint256 w = 0; w < x.length; w++) {
                if (!current(w)) continue;
                hit = hit || x[w] & CUR_T != 0;
                und = und || x[w] & CUR_D != 0;
            }
        }
        mu.pend = !hit && und;
        empty = !mu.pend;
    }

    /// Whether the operators from the fourth on are decided or undecided
    /// in the future X_D in word w.
    function current(uint256 w) internal pure returns (bool) {
        for (; w > 0; w /= 5) if (w % 5 > 1) return false;
        return true;
    }

    /// A new activation starts without candidates.
    function clear(Multi storage mu) internal {
        mu.gen++;
        mu.tail = 0;
        mu.h = 0;
        mu.pend = false;
    }

    function observe(Multi storage mu, EvalData storage data, uint256 n, int256 t) internal {
        mu.noted = n + 1;
        uint256 s = 0;
        for (uint256 i = 0; i < mu.ops.length; i++) {
            MOp storage op = mu.ops[i];
            uint256 v = op.mode == W_NESTED ? nested(mu, op, i, data, n, t) : decide(mu, op, i, data, n, t);
            s |= v << (2 * i);
        }
        mu.cur = s;
    }

    /// What observation n decides about operator i, for the candidates
    /// before n; returns its value for a candidate at n.
    function decide(Multi storage mu, MOp storage op, uint256 i, EvalData storage data, uint256 n, int256 t)
            internal returns (uint8 now_) {
        uint8 pv = code(CadlNormsEval.evaluate(data, op.tp, n, n));
        uint8 qv = op.mode == W_UNTIL ? code(CadlNormsEval.evaluate(data, op.tq, n, n)) : X_F;
        // the candidates whose window is over without a decisive value:
        // undefined when an operand was undefined in it
        uint256 x = firstFrom(mu, t - op.td);
        if (x > 0) {
            uint256 a = firstAfter(mu, op.uSet, op.u);
            assign(mu, i, X_U, a < x ? a : x);
            assign(mu, i, op.mode == W_THROUGHOUT ? X_T : X_F, x);
        }
        if (op.mode == W_WITHIN) {
            if (pv == X_T) assign(mu, i, X_T, mu.tail);
            if (pv == X_U) { op.uSet = true; op.u = n; }
            return pv == X_T ? X_T : X_D;
        }
        if (op.mode == W_THROUGHOUT) {
            if (pv == X_F) assign(mu, i, X_F, mu.tail);
            if (pv == X_U) { op.uSet = true; op.u = n; }
            return pv == X_F ? X_F : X_D;
        }
        // until(p, q): q true decides the candidates with p true since them,
        // and makes the others uncertain; p false ends every window
        if (qv == X_T) {
            assignRange(mu, i, X_T, firstAfter(mu, op.puSet, op.pu), mu.tail);
            if (op.puSet && (!op.uSet || op.u < op.pu)) { op.uSet = true; op.u = op.pu; }
        } else if (qv == X_U) {
            op.uSet = true;
            op.u = n;
        }
        if (pv == X_F) {
            uint256 a = firstAfter(mu, op.uSet, op.u);
            assign(mu, i, X_U, a);
            assign(mu, i, X_F, mu.tail);
        } else if (pv == X_U) {
            op.puSet = true;
            op.pu = n;
        }
        if (qv == X_T) return X_T;
        if (pv == X_F) return qv == X_U ? X_U : X_F;
        return X_D;
    }

    /// What observation n decides about the nested operator i. The inner
    /// value throughout(id, p) is decided for the observations [fed, b)
    /// whose window is over: undefined up to the last observation with p
    /// undefined, true after it (each of these windows ends at n - 1); and
    /// for [b, n] when p is false at n. Each run is fed to the outer
    /// operator in the order of the observations.
    function nested(Multi storage mu, MOp storage op, uint256 i, EvalData storage data, uint256 n, int256 t)
            internal returns (uint8 now_) {
        if (mu.tail == 0) { op.fed = n; op.iuSet = false; op.uSet = false; }
        uint8 pv = code(CadlNormsEval.evaluate(data, op.tp, n, n));
        if (op.ineg && pv != X_U) pv = pv == X_T ? X_F : X_T;
        uint256 f = op.fed;
        uint256 b = firstTime(data, f, n, t - op.id);
        if (b > f) {
            uint256 split = f;
            if (op.iuSet && op.iu >= f) split = op.iu + 1 < b ? op.iu + 1 : b;
            if (split > f) feed(mu, op, i, data, f, split - 1, X_U);
            if (b > split) feed(mu, op, i, data, split, b - 1, X_T);
        }
        if (pv == X_F) {
            uint8 y = feed(mu, op, i, data, b, n, X_F);
            if (y == decisive(op)) now_ = y;
            op.fed = n + 1;
        } else {
            if (pv == X_U) { op.iuSet = true; op.iu = n; }
            op.fed = b;
        }
        over(mu, op, i, op.fed <= n ? data.obsT[op.fed] : t);
    }

    /// The inner value x at the observations [s, e], fed to the outer
    /// operator: a decisive value decides the candidates whose window has
    /// one of these observations; an undefined one marks them.
    function feed(Multi storage mu, MOp storage op, uint256 i, EvalData storage data, uint256 s, uint256 e, uint8 x)
            internal returns (uint8 y) {
        y = op.oneg && x != X_U ? (x == X_T ? X_F : X_T) : x;
        int256 ts = data.obsT[s];
        over(mu, op, i, ts);
        if (y == decisive(op)) assignRange(mu, i, y, firstFrom(mu, ts - op.td), firstAfter(mu, true, e));
        else if (y == X_U) { op.uSet = true; op.u = e; }
    }

    /// The candidates whose outer window ends before time tau: every value
    /// of their window has been fed, so they are decided.
    function over(Multi storage mu, MOp storage op, uint256 i, int256 tau) internal {
        uint256 x = firstFrom(mu, tau - op.td);
        if (x == 0) return;
        uint256 a = firstAfter(mu, op.uSet, op.u);
        assign(mu, i, X_U, a < x ? a : x);
        assign(mu, i, op.outer == W_THROUGHOUT ? X_T : X_F, x);
    }

    function decisive(MOp storage op) internal view returns (uint8) {
        return op.outer == W_WITHIN ? X_T : X_F;
    }

    /// The first observation in [lo, hi) at or after time x, or hi.
    function firstTime(EvalData storage data, uint256 lo, uint256 hi, int256 x) internal view returns (uint256) {
        while (lo < hi) {
            uint256 mid = (lo + hi) / 2;
            if (data.obsT[mid] < x) lo = mid + 1; else hi = mid;
        }
        return lo;
    }

    /// Candidate n, with the values of the content computed at n.
    function add(Multi storage mu, EvalData storage data, uint256 n, int256 t) internal {
        uint256[] memory ops = new uint256[](mu.ops.length);
        for (uint256 i = 0; i < ops.length; i++) ops[i] = mu.ops[i].node;
        (uint256 ts, uint256 ds) = CadlNormsEval.table(data, mu.root, n, ops);
        if ((ts | ds) >> mu.cur & 1 == 0) return;     // decided, and not true
        insert(mu, n, t, leaf(ts, ds, mu.cur, ops.length, mu.words));
    }

    /// The leaf of a candidate whose operators are s now (2 bits each).
    function leaf(uint256 ts, uint256 ds, uint256 s, uint256 m, uint256 words) internal pure returns (uint256[] memory x) {
        x = new uint256[](words);
        uint256 count = 1 << (2 * m);
        for (uint256 e = 0; e < count; e++) {
            uint256 bits = (ts >> e & 1) | (ds >> e & 1) << 1;
            if (bits == 0) continue;
            (bool ok, uint256 p) = state(e, s, m);
            if (ok) x[p / 125] |= bits << (2 * (p % 125));
        }
    }

    /// The state of entry e for a candidate whose operators are s now, or
    /// !ok when e gives a decided operator another value.
    function state(uint256 e, uint256 s, uint256 m) internal pure returns (bool ok, uint256 p) {
        uint256 f = 1;
        for (uint256 i = 0; i < m; i++) {
            uint256 c = e >> (2 * i) & 3;
            uint256 si = s >> (2 * i) & 3;
            if (si == X_D) p += (1 + c) * f;
            else if (c != si) return (false, 0);
            f *= 5;
        }
        ok = true;
    }

    function insert(Multi storage mu, uint256 n, int256 t, uint256[] memory x) internal {
        uint256 c = mu.tail;
        // a new root above the old one when the tree is full (the loop
        // below writes it)
        if (c > 0 && c == 1 << mu.h) mu.h++;
        mu.k[key(mu, 255, c)] = n;
        mu.t[key(mu, 255, c)] = t;
        mu.tail = c + 1;
        store(mu, key(mu, 0, c), x);
        for (uint256 lvl = 1; lvl <= mu.h; lvl++) {
            // a node above the new leaf has no tags (its range is not full)
            uint256[] memory y = load(mu, key(mu, lvl - 1, (c >> (lvl - 1)) ^ 1));
            for (uint256 w = 0; w < x.length; w++) x[w] = (x[w] | y[w]) & BITS;
            store(mu, key(mu, lvl, c >> lvl), x);
        }
    }

    // --- the tree ----------------------------------------------------------------------

    function key(Multi storage mu, uint256 lvl, uint256 idx) internal view returns (uint256) {
        return mu.gen << 128 | lvl << 64 | idx;
    }

    function load(Multi storage mu, uint256 kk) internal view returns (uint256[] memory x) {
        uint256 words = mu.words;
        x = new uint256[](words);
        for (uint256 w = 0; w < words; w++) x[w] = mu.nodes[kk | w << 200];
    }

    /// Writes the words of a node (a zero word that was zero is not written).
    function store(Multi storage mu, uint256 kk, uint256[] memory x) internal {
        for (uint256 w = 0; w < x.length; w++) {
            uint256 slot = kk | w << 200;
            if (x[w] != 0 || mu.nodes[slot] != 0) mu.nodes[slot] = x[w];
        }
    }

    /// Operator i is v for the undecided candidates among [0, x).
    function assign(Multi storage mu, uint256 i, uint8 v, uint256 x) internal {
        assignRange(mu, i, v, 0, x);
    }

    /// Operator i is v for the undecided candidates among [lo, hi).
    function assignRange(Multi storage mu, uint256 i, uint8 v, uint256 lo, uint256 hi) internal {
        if (lo >= hi) return;
        uint256 kk = key(mu, mu.h, 0);
        uint256[] memory x = load(mu, kk);
        if (rec(mu, i, v, mu.h, 0, lo, hi, x)) store(mu, kk, x);
    }

    /// Node (lvl, idx) of value x: operator i is v for its undecided
    /// candidates in [lo, hi). Changes x; returns whether it changed.
    function rec(Multi storage mu, uint256 i, uint256 v, uint256 lvl, uint256 idx, uint256 lo, uint256 hi,
                 uint256[] memory x) internal returns (bool) {
        {
            uint256 l = idx << lvl;
            uint256 r = l + (1 << lvl);
            if (r <= lo || l >= hi || !undecided(x, i)) return false;
            if (lo <= l && r <= hi) {
                tag(x, i, v);
                return true;
            }
        }
        uint256[] memory a = load(mu, key(mu, lvl - 1, 2 * idx));
        uint256[] memory b = load(mu, key(mu, lvl - 1, 2 * idx + 1));
        bool pushed = push(mu, x, a, b);
        if (rec(mu, i, v, lvl - 1, 2 * idx, lo, hi, a) || pushed) store(mu, key(mu, lvl - 1, 2 * idx), a);
        if (rec(mu, i, v, lvl - 1, 2 * idx + 1, lo, hi, b) || pushed) store(mu, key(mu, lvl - 1, 2 * idx + 1), b);
        for (uint256 w = 0; w < x.length; w++) x[w] = (a[w] | b[w]) & BITS;
        return true;
    }

    /// The tags of the node x applied to its children a and b.
    function push(Multi storage mu, uint256[] memory x, uint256[] memory a, uint256[] memory b)
            internal view returns (bool pushed) {
        for (uint256 j = 0; j < mu.ops.length; j++) {
            uint256 tv = x[j / 3] >> (250 + 2 * (j % 3)) & 3;
            if (tv == 0) continue;
            tag(a, j, tv);
            tag(b, j, tv);
            pushed = true;
        }
    }

    /// Whether a candidate below the node x has operator i undecided.
    function undecided(uint256[] memory x, uint256 i) internal pure returns (bool) {
        if (i < 3) {
            uint256 und = BITS & ~keep(i);
            for (uint256 w = 0; w < x.length; w++) if (x[w] & und != 0) return true;
            return false;
        }
        uint256 st = 5 ** (i - 3);
        for (uint256 w = 0; w < x.length; w++) if (w / st % 5 != 0 && x[w] & BITS != 0) return true;
        return false;
    }

    function keep(uint256 i) internal pure returns (uint256) {
        return i == 0 ? KEEP0 : i == 1 ? KEEP1 : KEEP2;
    }

    /// The node x with operator i decided to v for its undecided
    /// candidates: the states with s_i = 1 + v move to s_i = 0, the other
    /// undecided ones go.
    function tag(uint256[] memory x, uint256 i, uint256 v) internal pure {
        if (!undecided(x, i)) return;
        if (i < 3) {
            uint256 kp = keep(i);
            uint256 sh = 2 * (1 + v) * 5 ** i;
            for (uint256 w = 0; w < x.length; w++) {
                uint256 y = x[w];
                x[w] = (y & ~BITS) | (y & kp) | ((y & BITS) >> sh & kp);
            }
        } else {
            uint256 st = 5 ** (i - 3);
            for (uint256 w = 0; w < x.length; w++) {
                if (w / st % 5 == 0) x[w] |= x[w + (1 + v) * st] & BITS;
            }
            for (uint256 w = 0; w < x.length; w++) {
                if (w / st % 5 != 0) x[w] &= ~BITS;
            }
        }
        x[i / 3] |= v << (250 + 2 * (i % 3));
    }

    // --- the candidates ----------------------------------------------------------------

    /// The first candidate with a time at or after x, or tail.
    function firstFrom(Multi storage mu, int256 x) internal view returns (uint256) {
        uint256 lo = 0;
        uint256 hi = mu.tail;
        while (lo < hi) {
            uint256 mid = (lo + hi) / 2;
            if (mu.t[key(mu, 255, mid)] < x) lo = mid + 1; else hi = mid;
        }
        return lo;
    }

    /// The first candidate at an observation after u (all when !set).
    function firstAfter(Multi storage mu, bool set, uint256 u) internal view returns (uint256) {
        if (!set) return 0;
        uint256 lo = 0;
        uint256 hi = mu.tail;
        while (lo < hi) {
            uint256 mid = (lo + hi) / 2;
            if (mu.k[key(mu, 255, mid)] <= u) lo = mid + 1; else hi = mid;
        }
        return lo;
    }

    function code(Val memory v) internal pure returns (uint8) {
        if (v.k == K_B || v.k == K_N) return v.n != 0 ? X_T : X_F;
        return X_U;
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

// states and verdicts of a norm instance
uint8 constant INACTIVE = 0;
uint8 constant ACTIVE = 1;
uint8 constant SUSPENDED = 2;
uint8 constant FULFILLED = 3;
uint8 constant VIOLATED = 4;
uint8 constant DISCHARGED = 5;
uint8 constant V_PENDING = 0;
uint8 constant V_FULFILLED = 1;
uint8 constant V_VIOLATED = 2;
uint8 constant V_EXCUSED = 3;

// kinds of norms and terms
uint8 constant OBLIGATION = 0;
uint8 constant PERMISSION = 1;
uint8 constant PROHIBITION = 2;
uint8 constant POWER = 3;
uint8 constant T_EVENT = 1;
uint8 constant T_MESSAGE = 2;
uint8 constant T_PREDICATE = 3;
uint8 constant T_ERROR = 4;

struct Pat { string name; uint8 ik; string idx; bool hasRange; int256 lo; int256 hi; }
struct Term { uint8 kind; string text; uint256 root; Pat sender; Pat receiver; string msgName; bool hasArgs; uint256[] args; }
struct NormDef {
    string id; uint8 kind; bool frozen; int256 trigger; int256 until; int256 content;
    bool hasDeadline; int256 deadline; bool target; bool hasBearer; Pat bearer;
    string severity; bool hasSanction; string sanction; int256 reparation;
    bool hasReparationId; string reparationId; bool hasTransition; string transition;
    uint256[] activate; uint256[] suspend; uint256[] resume; uint256[] discharge;
    // a content with one temporal operator (1, see `temporals`), two or
    // more or a nested one (2, see `multis`), or none (0)
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

// The model of the CADL contract (filled by the constructor of the
// generated contract) and the state of the contract instance, in one
// struct, so that the libraries below can read and change them.
struct Monitor {
    // --- the model
    EvalData data;
    Term[] terms;
    NormDef[] normDefs;
    Rule[] rules;
    Item[] assumeItems;
    Item[] guaranteeItems;
    mapping(bytes32 => ActorDecl) actors;
    string lcInitial;
    mapping(bytes32 => bool) lcStates;
    mapping(bytes32 => bool) lcTerminal;
    // the bare names read inside a temporal operator (see makeKnown)
    mapping(bytes32 => bool) temporalName;
    // by norm, for a content with a temporal operator: the operator, and
    // the candidates that are true with it and those that are never true
    mapping(uint256 => Temporal) temporals;
    mapping(uint256 => Windows) windows;
    // by norm, for a content with two or more temporal operators or a
    // nested one
    mapping(uint256 => Multi) multis;
    // --- the state of the contract instance
    uint256 observations;      // observations of the instance so far
    int256 lastT;
    bool hasLastT;
    bool ended;
    uint256 ignored;           // observations after the end
    bool begun;
    string lc;                 // lifecycle state after the last observation
    NormRec[] recs;
    ItemRec[] assumeRecs;
    ItemRec[] guaranteeRecs;
    bool failed;               // an assumption has failed
    int256 failedAt;
}

// What every part of the monitor shares (free functions: the code is part of
// each contract and library that uses them).
function setSt(Monitor storage m, uint256 i, uint8 st, uint256 n, int256 t) {
    NormRec storage r = m.recs[i];
    if (r.state == ACTIVE && r.hasSince) r.before += t - r.since;
    r.state = st;
    if (st == ACTIVE) {
        r.since = t;
        r.hasSince = true;
        if (!r.hasAt) { r.hasAt = true; r.activatedAt = t; r.act = int256(n); }
    }
    if (st == VIOLATED) r.verdict = m.failed ? V_EXCUSED : V_VIOLATED;
    else if (st == FULFILLED || st == DISCHARGED) r.verdict = V_FULFILLED;
    if (st >= FULFILLED) {
        CadlNormsWindows.clear(m.windows[i]);
        CadlNormsMulti.clear(m.multis[i]);
    }
}

function elapsed(Monitor storage m, uint256 i, int256 t) view returns (int256) {
    NormRec storage r = m.recs[i];
    if (r.state == ACTIVE && r.hasSince) return r.before + (t - r.since);
    return r.before;
}

function truthOf(Val memory v) pure returns (Val memory) {
    if (v.k == K_U || v.k == K_D || v.k == K_B) return v;
    if (v.k == K_N) return Val(K_B, v.n != 0 ? int256(1) : int256(0), "");
    return Val(K_U, 0, "");
}

function evalTruth(Monitor storage m, uint256 root, uint256 p, uint256 n) returns (Val memory) {
    return truthOf(CadlNormsEval.evaluate(m.data, root, p, n));
}

// Events, messages, actors, counts-as rules and powers (F.3, F.5 steps 1
// to 3), in a library of their own.
library CadlNormsActs {
    event InstitutionalEvent(string name, string performer, string source, int256 t);
    event PowerExercised(string power, string performer, string act, int256 t);
    event Unauthorised(string act, string performer, int256 t);

    /// Steps 1 to 3 of an observation: the counts-as rules, the powers
    /// and the triggers. Returns the institutional events created.
    function acts(Monitor storage m, uint256 n, int256 t, Msg[] calldata msgs) public returns (Ev[] memory created) {
        created = countsAs(m, n, t, msgs);
        powers(m, n, t, msgs, created);
        for (uint256 i = 0; i < m.normDefs.length; i++) {
            NormDef storage d = m.normDefs[i];
            if (d.frozen || d.trigger < 0 || !occurs(m, uint256(d.trigger), n, msgs, created)) continue;
            if (m.recs[i].state == INACTIVE) setSt(m, i, ACTIVE, n, t);
        }
    }

    function countsAs(Monitor storage m, uint256 n, int256 t, Msg[] calldata msgs) internal returns (Ev[] memory created) {
        Ev[] memory buf = new Ev[](m.rules.length * (msgs.length + 1));
        uint256 c = 0;
        Ev[] memory none = new Ev[](0);
        for (uint256 i = 0; i < m.rules.length; i++) {
            Rule storage r = m.rules[i];
            if (r.broken) continue;
            if (r.cond >= 0) {
                Val memory cv = evalTruth(m, m.terms[uint256(r.cond)].root, n, n);
                if (!(cv.k == K_B && cv.n == 1)) continue;
            }
            Term storage w = m.terms[uint256(r.when)];
            if (w.kind == T_MESSAGE) {
                for (uint256 j = 0; j < msgs.length; j++) {
                    if (!msgMatches(m, w, msgs[j], n)) continue;
                    buf[c++] = Ev(r.creates, true, msgs[j].from, desc(msgs[j]));
                }
            } else if (occurs(m, uint256(r.when), n, msgs, none)) {
                buf[c++] = Ev(r.creates, false, "", w.text);
            }
        }
        created = new Ev[](c);
        for (uint256 i = 0; i < c; i++) {
            created[i] = buf[i];
            emit InstitutionalEvent(buf[i].name, buf[i].performer, buf[i].source, t);
        }
    }

    function desc(Msg calldata x) internal pure returns (string memory) {
        return string.concat(x.from, " -> ", x.to, " : ", x.name);
    }

    function occurs(Monitor storage m, uint256 j, uint256 n, Msg[] calldata msgs, Ev[] memory created) public returns (bool) {
        Term storage term = m.terms[j];
        if (term.kind == T_EVENT) {
            bytes32 h = keccak256(bytes(term.text));
            for (uint256 e = 0; e < created.length; e++) if (keccak256(bytes(created[e].name)) == h) return true;
            return false;
        }
        if (term.kind == T_MESSAGE) {
            for (uint256 k = 0; k < msgs.length; k++) if (msgMatches(m, term, msgs[k], n)) return true;
            return false;
        }
        if (term.kind == T_PREDICATE) {
            Val memory now_ = evalTruth(m, term.root, n, n);
            if (!(now_.k == K_B && now_.n == 1)) return false;
            if (n == 0) return true;
            Val memory prev = evalTruth(m, term.root, n - 1, n - 1);
            return !(prev.k == K_B && prev.n == 1);
        }
        return false;
    }

    // --- powers (step 2) -------------------------------------------------------------

    function powers(Monitor storage m, uint256 n, int256 t, Msg[] calldata msgs, Ev[] memory created) internal {
        Act[] memory list = collectActs(m, n, msgs, created);
        bool[] memory active = new bool[](m.normDefs.length);
        for (uint256 p = 0; p < m.normDefs.length; p++) active[p] = m.recs[p].state == ACTIVE;
        for (uint256 a = 0; a < list.length; a++) exercise(m, list[a], active, n, t);
    }

    function eventUsedByPower(Monitor storage m, string memory name) internal view returns (bool) {
        bytes32 h = keccak256(bytes(name));
        for (uint256 p = 0; p < m.normDefs.length; p++) {
            NormDef storage d = m.normDefs[p];
            if (d.kind == POWER && !d.frozen && m.terms[uint256(d.content)].kind == T_EVENT
                && keccak256(bytes(m.terms[uint256(d.content)].text)) == h) return true;
        }
        return false;
    }

    function collectActs(Monitor storage m, uint256 n, Msg[] calldata msgs, Ev[] memory created) internal returns (Act[] memory list) {
        Act[] memory buf = new Act[](created.length + m.normDefs.length * msgs.length);
        uint256 c = 0;
        for (uint256 e = 0; e < created.length; e++) {
            if (created[e].hasPerformer && eventUsedByPower(m, created[e].name)) {
                buf[c++] = Act(created[e].name, created[e].performer,
                               string.concat("institutional event ", created[e].name));
            }
        }
        uint256 firstMsgAct = c;
        for (uint256 p = 0; p < m.normDefs.length; p++) {
            NormDef storage d = m.normDefs[p];
            if (d.kind != POWER || d.frozen || m.terms[uint256(d.content)].kind != T_MESSAGE) continue;
            for (uint256 k = 0; k < msgs.length; k++) {
                if (!msgMatches(m, m.terms[uint256(d.content)], msgs[k], n)) continue;
                Act memory act = Act(m.terms[uint256(d.content)].text, msgs[k].from,
                                     string.concat("message ", desc(msgs[k])));
                if (!seenAct(buf, firstMsgAct, c, act)) buf[c++] = act;
            }
        }
        list = new Act[](c);
        for (uint256 i = 0; i < c; i++) list[i] = buf[i];
    }

    function seenAct(Act[] memory buf, uint256 from, uint256 to, Act memory act) internal pure returns (bool) {
        for (uint256 q = from; q < to; q++) {
            if (keccak256(bytes(buf[q].content)) == keccak256(bytes(act.content))
                && keccak256(bytes(buf[q].desc)) == keccak256(bytes(act.desc))) return true;
        }
        return false;
    }

    function exercise(Monitor storage m, Act memory act, bool[] memory active, uint256 n, int256 t) internal {
        bool any = false;
        bytes32 h = keccak256(bytes(act.content));
        for (uint256 p = 0; p < m.normDefs.length; p++) {
            NormDef storage d = m.normDefs[p];
            if (d.kind != POWER || d.frozen || !active[p]) continue;
            if (keccak256(bytes(m.terms[uint256(d.content)].text)) != h) continue;
            if (!isBearer(m, p, act.performer)) continue;
            any = true;
            emit PowerExercised(d.id, act.performer, act.desc, t);
            applyEffects(m, d.activate, INACTIVE, INACTIVE, ACTIVE, n, t);
            applyEffects(m, d.suspend, ACTIVE, ACTIVE, SUSPENDED, n, t);
            applyEffects(m, d.resume, SUSPENDED, SUSPENDED, ACTIVE, n, t);
            applyEffects(m, d.discharge, ACTIVE, SUSPENDED, DISCHARGED, n, t);
        }
        if (!any) emit Unauthorised(act.desc, act.performer, t);
    }

    function applyEffects(Monitor storage m, uint256[] storage targets, uint8 from1, uint8 from2, uint8 to, uint256 n, int256 t) internal {
        for (uint256 k = 0; k < targets.length; k++) {
            uint256 x = targets[k];
            if (m.normDefs[x].frozen) continue;
            uint8 s = m.recs[x].state;
            if (s == from1 || s == from2) setSt(m, x, to, n, t);
        }
    }

    function isBearer(Monitor storage m, uint256 p, string memory performer) internal view returns (bool) {
        NormDef storage d = m.normDefs[p];
        if (!d.hasBearer) return false;
        (string memory text, bool bound) = actorText(m, d.bearer.name, d.bearer.ik, d.bearer.idx);
        if (bound) return keccak256(bytes(performer)) == keccak256(bytes(text));
        (string memory name, bool hasIdx, string memory idx) = CadlNormsValues.splitActor(performer);
        return keccak256(bytes(name)) == keccak256(bytes(d.bearer.name)) && isMember(m, name, hasIdx, idx);
    }

    // --- messages and actors -----------------------------------------------------------

    function msgMatches(Monitor storage m, Term storage term, Msg calldata x, uint256 n) internal returns (bool) {
        if (keccak256(bytes(x.name)) != keccak256(bytes(term.msgName))) return false;
        if (!actorMatches(m, term.sender, x.from) || !actorMatches(m, term.receiver, x.to)) return false;
        if (!term.hasArgs) return true;
        if (term.args.length != x.args.length) return false;
        for (uint256 a = 0; a < term.args.length; a++) {
            Val memory w = CadlNormsEval.evaluate(m.data, term.args[a], n, n);
            if (!eqRaw(w, x.args[a])) return false;
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

    function actorMatches(Monitor storage m, Pat storage pat, string calldata actual) internal view returns (bool) {
        (string memory name, bool hasIdx, string memory idx) = CadlNormsValues.splitActor(actual);
        if (keccak256(bytes(name)) != keccak256(bytes(pat.name)) || !isMember(m, name, hasIdx, idx)) return false;
        if (pat.ik == I_ANY) {
            if (!hasIdx) return false;
            if (!pat.hasRange) return true;
            int256 v = CadlNormsValues.parseIndex(idx);
            return v >= pat.lo && v <= pat.hi;
        }
        (string memory text, bool bound) = actorText(m, pat.name, pat.ik, pat.idx);
        if (bound) return keccak256(bytes(actual)) == keccak256(bytes(text));
        return true;
    }

    function isMember(Monitor storage m, string memory name, bool hasIdx, string memory idx) internal view returns (bool) {
        ActorDecl storage a = m.actors[keccak256(bytes(name))];
        if (!a.declared) return false;
        if (!a.isIndexed || !hasIdx) return !a.isIndexed && !hasIdx;
        if (!CadlNormsValues.isDigits(idx)) return false;
        int256 v = CadlNormsValues.parseIndex(idx);
        return (!a.hasLo || a.lo <= v) && (!a.hasHi || v <= a.hi);
    }

    /// The text of an actor reference with its index bound; bound is false
    /// for an index name that the instance does not bind.
    function actorText(Monitor storage m, string memory name, uint8 ik, string memory idx) internal view returns (string memory, bool) {
        if (ik == I_NONE) return (name, true);
        if (ik == I_LIT) return (string.concat(name, "[", idx, "]"), true);
        bytes32 h = keccak256(bytes(idx));
        if (m.data.hasBinding[h]) {
            Stored storage b = m.data.bindings[h];
            string memory text = b.k == K_T ? b.s : CadlNormsValues.intText(b.n / SCALE);
            return (string.concat(name, "[", text, "]"), true);
        }
        return (string.concat(name, "[", idx, "]"), false);
    }
}

abstract contract CadlNormsMonitorBase {
    // --- the model (filled by the constructor of the generated contract) and the state
    string public contractId;
    Monitor internal m;

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

    event NormChanged(uint256 index, string id, uint8 state, uint8 verdict, uint8 previousState, uint8 previousVerdict, int256 t);
    event ItemChanged(bool guarantee, uint256 index, uint8 mark, uint8 previous, int256 t);
    event Violation(string norm, string severity, uint8 verdict, string sanction, string reparation, bool reparationActivated, string transition, int256 t);
    event InstitutionalEvent(string name, string performer, string source, int256 t);
    event PowerExercised(string power, string performer, string act, int256 t);
    event Unauthorised(string act, string performer, int256 t);

    // =========================================================================
    // The interface
    // =========================================================================

    /// Observations of the instance so far.
    function observations() external view returns (uint256) { return m.observations; }
    /// The time of the last observation, also of one after the end.
    function lastT() external view returns (int256) { return m.lastT; }
    function ended() external view returns (bool) { return m.ended; }
    /// Observations after the end.
    function ignored() external view returns (uint256) { return m.ignored; }

    /// Bindings, request fields (by path, "a.b") and names of quantities,
    /// before the first observation. Optional; may be called once.
    function begin(ValIn[] calldata bindings_, ValIn[] calldata request_, string[] calldata quantities) external onlyReporter {
        require(m.observations == 0 && !m.begun && !m.ended, "begin() comes before the first observation");
        m.begun = true;
        CadlNormsValues.checkBegin(bindings_, request_);
        for (uint256 i = 0; i < bindings_.length; i++) {
            ValIn calldata b = bindings_[i];
            bytes32 h = keccak256(bytes(b.name));
            m.data.hasBinding[h] = true;
            m.data.bindings[h] = Stored(0, b.k, b.n, b.s);
        }
        for (uint256 i = 0; i < request_.length; i++) {
            ValIn calldata r = request_[i];
            bytes32 h = keccak256(bytes(r.name));
            m.data.hasRequest[h] = true;
            m.data.request[h] = Stored(0, r.k, r.n, r.s);
        }
        for (uint256 i = 0; i < quantities.length; i++) {
            m.data.known[keccak256(bytes(CadlNormsValues.canon(quantities[i])))] = true;
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
        if (m.data.known[h]) return;
        require(m.observations == 0 || !m.temporalName[h] || m.data.hasBinding[h],
                "a name used inside a temporal operator becomes a quantity after the first observation; give it in begin()");
        m.data.known[h] = true;
    }

    /// One observation (F.5): its time (seconds * SCALE), the values that
    /// change, the messages sent, the lifecycle state after it (when
    /// hasState) and whether the instance ends at it. Reverts, leaving the
    /// monitor as it was, for an observation that is malformed.
    function observe(int256 t, ValIn[] calldata values, Msg[] calldata msgs,
                     bool hasState, string calldata state, bool end) external onlyReporter {
        require(t >= -MAX_T && t <= MAX_T, "'t' must be a finite number of at most 2**53 in absolute value");
        require(!m.hasLastT || t >= m.lastT, "time is before the previous observation");
        CadlNormsValues.checkObservation(values, msgs);
        m.lastT = t;
        m.hasLastT = true;
        if (m.ended) {
            m.ignored += 1;
            return;
        }
        bool endHere = record(t, values, hasState, state, end);
        runStep(m.observations - 1, t, msgs, endHere);
    }

    function runStep(uint256 n, int256 t, Msg[] calldata msgs, bool endHere) internal {
        (uint8[] memory ns, uint8[] memory nv, uint8[] memory am, uint8[] memory gm) = marks();
        step(n, t, msgs, endHere);
        emitChanges(ns, nv, am, gm, t);
        // the first observation that a later step can read: this one, and
        // the first while a temporal item is not decided (the candidates of
        // a temporal content keep what they need in their window)
        m.data.keepFrom = n > 0 && CadlNormsItems.pending(m.assumeItems, m.assumeRecs, m.guaranteeItems,
                                                          m.guaranteeRecs) ? 0 : n;
    }

    function record(int256 t, ValIn[] calldata values, bool hasState, string calldata state, bool end)
            internal returns (bool) {
        uint256 n = m.observations;
        string memory before = n == 0 ? m.lcInitial : m.lc;
        string memory after_ = hasState ? state : before;
        if (m.data.hasLifecycle) {
            require(m.lcStates[keccak256(bytes(after_))], "the state is not a state of the lifecycle");
        }
        for (uint256 i = 0; i < values.length; i++) {
            bytes32 h = keccak256(bytes(CadlNormsValues.canon(values[i].name)));
            makeKnown(h);
            prune(h);
            m.data.history[h].push(Stored(n, values[i].k, values[i].n, values[i].s));
        }
        m.data.obsT.push(t);
        m.data.obsSt.push(before);
        m.lc = after_;
        m.observations = n + 1;
        return end || (m.data.hasLifecycle && m.lcTerminal[keccak256(bytes(after_))]);
    }

    /// Drop values of a quantity that no lookup can read any more: at most
    /// four per new value, so that the cost of an observation stays bounded
    /// when a long window closes (the rest goes with the next values).
    function prune(bytes32 h) internal {
        Stored[] storage hs = m.data.history[h];
        uint256 first = m.data.head[h];
        uint256 stop = first + 4;
        while (first < stop && first + 1 < hs.length && hs[first + 1].idx <= m.data.keepFrom) {
            delete hs[first];
            first++;
        }
        m.data.head[h] = first;
    }

    /// The end of the contract instance at its last observation (F.5,
    /// item 6), when no observation said so.
    function finish() external onlyReporter {
        require(m.observations > 0, "the contract instance has no observation");
        if (m.ended) {
            return;
        }
        (uint8[] memory ns, uint8[] memory nv, uint8[] memory am, uint8[] memory gm) = marks();
        uint256 n = m.observations - 1;
        settle(n, m.data.obsT[n]);
        finishItems(true);
        emitChanges(ns, nv, am, gm, m.data.obsT[n]);
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
        ids = new string[](m.normDefs.length);
        for (uint256 i = 0; i < m.normDefs.length; i++) ids[i] = m.normDefs[i].id;
    }

    /// False when a norm or a guarantee is violated.
    function ok() external view returns (bool) {
        for (uint256 i = 0; i < m.recs.length; i++) if (m.recs[i].verdict == V_VIOLATED) return false;
        for (uint256 i = 0; i < m.guaranteeRecs.length; i++) if (m.guaranteeRecs[i].verdict == V_VIOLATED) return false;
        return true;
    }

    /// The earliest deadline of an active obligation or prohibition
    /// (seconds * SCALE); has is false when there is none.
    function nextDeadline() external view returns (bool has, int256 due) {
        if (m.ended || m.observations == 0) return (false, 0);
        for (uint256 i = 0; i < m.normDefs.length; i++) {
            NormDef storage d = m.normDefs[i];
            NormRec storage r = m.recs[i];
            if (d.frozen || !d.hasDeadline || r.state != ACTIVE || (d.kind != OBLIGATION && d.kind != PROHIBITION)) continue;
            int256 el = elapsed(m, i, m.lastT);
            if (el > d.deadline) continue;
            int256 at_ = m.lastT + d.deadline - el;
            if (!has || at_ < due) { has = true; due = at_; }
        }
    }

    // =========================================================================
    // One observation (F.5, "Order within one observation")
    // =========================================================================

    function step(uint256 n, int256 t, Msg[] calldata msgs, bool endHere) internal {
        if (n == 0) {
            for (uint256 i = 0; i < m.normDefs.length; i++) {
                NormDef storage d = m.normDefs[i];
                if (!d.frozen && d.trigger < 0 && !d.target) setSt(m, i, ACTIVE, n, t);
            }
        }
        bool was = m.failed;
        m.failed = CadlNormsItems.step(m.assumeItems, m.assumeRecs, m.guaranteeItems, m.guaranteeRecs, m.data, n, was);
        if (m.failed && !was) m.failedAt = t;
        Ev[] memory created = CadlNormsActs.acts(m, n, t, msgs);
        for (uint256 i = 0; i < m.normDefs.length; i++) {
            NormDef storage d = m.normDefs[i];
            if (d.tmode == 0 || d.frozen) continue;
            if (d.tmode == 1) CadlNormsWindows.note(m.windows[i], m.data, m.temporals[i], n, t);
            else CadlNormsMulti.note(m.multis[i], m.data, n, t);
        }
        uint256[] memory violated = judge(n, t, msgs, created);
        for (uint256 v = 0; v < violated.length; v++) onViolation(violated[v], n, t, true);
        if (endHere) settle(n, t);
        finishItems(endHere);
    }

    function finishItems(bool endHere) internal {
        if (endHere) m.ended = true;
        CadlNormsItems.finish(m.assumeItems, m.assumeRecs, m.guaranteeItems, m.guaranteeRecs, endHere);
    }

    // --- contents, deadlines and until (step 4) -----------------------------------------

    function judge(uint256 n, int256 t, Msg[] calldata msgs, Ev[] memory created) internal returns (uint256[] memory violated) {
        uint256[] memory buf = new uint256[](m.normDefs.length);
        uint256 c = 0;
        for (uint256 i = 0; i < m.normDefs.length; i++) {
            NormDef storage d = m.normDefs[i];
            if (d.frozen || n < m.recs[i].from) continue;
            if (m.recs[i].state == ACTIVE && (d.kind == OBLIGATION || d.kind == PROHIBITION)
                && judgeNorm(i, n, t, msgs, created)) buf[c++] = i;
            uint8 st = m.recs[i].state;
            if ((st == ACTIVE || st == SUSPENDED) && d.until >= 0
                && CadlNormsActs.occurs(m, uint256(d.until), n, msgs, created)) {
                setSt(m, i, DISCHARGED, n, t);
            }
        }
        violated = new uint256[](c);
        for (uint256 i = 0; i < c; i++) violated[i] = buf[i];
    }

    /// The content of an active obligation or prohibition at observation
    /// n: true when the norm is violated.
    function judgeNorm(uint256 i, uint256 n, int256 t, Msg[] calldata msgs, Ev[] memory created) internal returns (bool) {
        NormDef storage d = m.normDefs[i];
        bool open = !d.hasDeadline || elapsed(m, i, t) <= d.deadline;
        bool hit;
        bool closed;
        if (d.tmode != 0) {
            // a content with a temporal operator (see CadlNormsWindows)
            (hit, closed) = judgeWindow(i, n, t, open);
        } else {
            // the content has no temporal operator: it is decided now
            hit = open && contentHolds(i, n, msgs, created);
            closed = !open;
        }
        // a hit fulfils an obligation and violates a prohibition; a closed
        // window without one does the opposite
        bool obligation = d.kind == OBLIGATION;
        if (!hit && !closed) return false;
        setSt(m, i, hit == obligation ? FULFILLED : VIOLATED, n, t);
        return hit != obligation;
    }

    function judgeWindow(uint256 i, uint256 n, int256 t, bool open) internal returns (bool hit, bool closed) {
        bool empty;
        if (m.normDefs[i].tmode == 1) (hit, empty) = CadlNormsWindows.judge(m.windows[i], m.data, m.temporals[i], n, t, open);
        else (hit, empty) = CadlNormsMulti.judge(m.multis[i], m.data, n, t, open);
        closed = !hit && !open && empty;
    }

    function contentHolds(uint256 i, uint256 n, Msg[] calldata msgs, Ev[] memory created) internal returns (bool) {
        uint256 j = uint256(m.normDefs[i].content);
        if (m.terms[j].kind == T_PREDICATE) {
            Val memory v = evalTruth(m, m.terms[j].root, n, n);
            return v.k == K_B && v.n == 1;
        }
        return CadlNormsActs.occurs(m, j, n, msgs, created);
    }

    // --- violations (step 5) and the end (step 6) ------------------------------------------

    function onViolation(uint256 i, uint256 n, int256 t, bool activate) internal {
        NormDef storage d = m.normDefs[i];
        bool repActivated = false;
        if (activate && d.reparation >= 0) {
            uint256 rp = uint256(d.reparation);
            if (!m.normDefs[rp].frozen && m.recs[rp].state == INACTIVE) repActivated = true;
        }
        string memory transition = "";
        bool terminal = m.data.hasLifecycle && m.lcTerminal[keccak256(bytes(m.data.obsSt[n]))];
        if (d.hasTransition && activate && !terminal) transition = d.transition;
        emit Violation(d.id, d.severity, m.recs[i].verdict, d.sanction, d.reparationId, repActivated, transition, t);
        if (repActivated) {
            uint256 rp = uint256(d.reparation);
            m.recs[rp].from = n + 1;
            setSt(m, rp, ACTIVE, n, t);
        }
    }

    function settle(uint256 n, int256 t) internal {
        m.ended = true;
        uint256[] memory late = new uint256[](m.normDefs.length);
        uint256 c = 0;
        for (uint256 i = 0; i < m.normDefs.length; i++) {
            NormDef storage d = m.normDefs[i];
            NormRec storage r = m.recs[i];
            if (d.frozen || r.state >= FULFILLED) continue;
            bool inForce = r.state == ACTIVE || r.state == SUSPENDED;
            if (inForce && r.act == int256(n)) { setSt(m, i, DISCHARGED, n, t); continue; }
            if (inForce && (d.kind == OBLIGATION || d.kind == PROHIBITION)
                && (CadlNormsWindows.pending(m.windows[i]) || m.multis[i].pend)) continue;
            if (inForce && d.kind == OBLIGATION) {
                if (!d.hasDeadline) { setSt(m, i, VIOLATED, n, t); late[c++] = i; }
                else if (elapsed(m, i, t) <= d.deadline) setSt(m, i, DISCHARGED, n, t);
                else { setSt(m, i, VIOLATED, n, t); late[c++] = i; }
            } else if (inForce && d.kind == PROHIBITION) {
                setSt(m, i, FULFILLED, n, t);
            } else {
                setSt(m, i, DISCHARGED, n, t);
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
        ns = new uint8[](m.recs.length);
        nv = new uint8[](m.recs.length);
        for (uint256 i = 0; i < m.recs.length; i++) { ns[i] = m.recs[i].state; nv[i] = m.recs[i].verdict; }
        am = new uint8[](m.assumeRecs.length);
        for (uint256 i = 0; i < m.assumeRecs.length; i++) am[i] = itemMark(m.assumeRecs[i], false);
        gm = new uint8[](m.guaranteeRecs.length);
        for (uint256 i = 0; i < m.guaranteeRecs.length; i++) gm[i] = itemMark(m.guaranteeRecs[i], true);
    }

    function emitChanges(uint8[] memory ns, uint8[] memory nv, uint8[] memory am, uint8[] memory gm, int256 t) internal {
        for (uint256 i = 0; i < m.recs.length; i++) {
            if (m.recs[i].state != ns[i] || m.recs[i].verdict != nv[i]) {
                emit NormChanged(i, m.normDefs[i].id, m.recs[i].state, m.recs[i].verdict, ns[i], nv[i], t);
            }
        }
        for (uint256 i = 0; i < m.assumeRecs.length; i++) {
            uint8 k = itemMark(m.assumeRecs[i], false);
            if (k != am[i]) emit ItemChanged(false, i + 1, k, am[i], t);
        }
        for (uint256 i = 0; i < m.guaranteeRecs.length; i++) {
            uint8 k = itemMark(m.guaranteeRecs[i], true);
            if (k != gm[i]) emit ItemChanged(true, i + 1, k, gm[i], t);
        }
    }

    // =========================================================================
    // Filling the model (used by the constructor of the generated contract)
    // =========================================================================

    function _node(uint8 op, uint8 bop, uint256 a, uint256 b, int256 d, uint8 ik,
                   string memory s1, string memory s2, string memory s3, uint8 lk, int256 ln, string memory ls) internal {
        Node storage nd = m.data.nodes.push();
        nd.op = op; nd.bop = bop; nd.a = a; nd.b = b; nd.d = d; nd.ik = ik;
        nd.s1 = s1; nd.s2 = s2; nd.s3 = s3;
        nd.lit = Val(lk, ln, ls);
    }

    function _item(uint256 i) internal {
        m.data.nodes[m.data.nodes.length - 1].items.push(i);
    }

    function _pat(Pat storage p, string memory name, uint8 ik, string memory idx, bool hasRange, int256 lo, int256 hi) internal {
        p.name = name; p.ik = ik; p.idx = idx; p.hasRange = hasRange; p.lo = lo; p.hi = hi;
    }

    function _term(uint8 kind, string memory text, uint256 root, string memory msgName, bool hasArgs) internal returns (Term storage t) {
        t = m.terms.push();
        t.kind = kind; t.text = text; t.root = root; t.msgName = msgName; t.hasArgs = hasArgs;
    }

    function _norm(string memory id, uint8 kind, bool frozen, int256 trigger, int256 until, int256 content,
                   bool hasDeadline, int256 deadline, bool target) internal returns (NormDef storage d) {
        d = m.normDefs.push();
        d.id = id; d.kind = kind; d.frozen = frozen; d.trigger = trigger; d.until = until; d.content = content;
        d.hasDeadline = hasDeadline; d.deadline = deadline; d.target = target;
        d.reparation = -1;
        NormRec storage r = m.recs.push();
        r.act = -1;
    }

    function _temporal(uint8 mode, uint256 tp, uint256 tq, int256 td, uint256 node, uint256 root) internal {
        uint256 i = m.normDefs.length - 1;
        m.normDefs[i].tmode = 1;
        m.temporals[i] = Temporal(mode, tp, tq, td, node, root);
    }

    /// The content of the last norm has the temporal operators that the
    /// calls of _op and _nested add; words: the storage words of a node.
    function _multi(uint256 root, uint256 words) internal {
        uint256 i = m.normDefs.length - 1;
        m.normDefs[i].tmode = 2;
        m.multis[i].root = root;
        m.multis[i].words = words;
    }

    function _op(uint8 mode, uint256 tp, uint256 tq, int256 td, uint256 node) internal {
        MOp storage op = m.multis[m.normDefs.length - 1].ops.push();
        op.mode = mode; op.tp = tp; op.tq = tq; op.td = td; op.node = node;
    }

    /// outer (within or throughout) with duration td on throughout(id, p)
    /// (p negated when ineg), the inner value negated when oneg.
    function _nested(uint8 outer, int256 td, uint256 node, uint256 tp, bool ineg, bool oneg, int256 id) internal {
        MOp storage op = m.multis[m.normDefs.length - 1].ops.push();
        op.mode = W_NESTED; op.outer = outer; op.td = td; op.node = node;
        op.tp = tp; op.ineg = ineg; op.oneg = oneg; op.id = id;
    }

    function _actor(string memory name, bool isIndexed_, bool hasLo, int256 lo, bool hasHi, int256 hi) internal {
        m.actors[keccak256(bytes(name))] = ActorDecl(true, isIndexed_, hasLo, lo, hasHi, hi);
    }

    function _assume(string memory text, bool temporal_, bool broken, bool unsupported, uint256 root) internal {
        m.assumeItems.push(Item(text, temporal_, broken, unsupported, root));
        m.assumeRecs.push(ItemRec(0, false, V_PENDING));
    }

    function _guarantee(string memory text, bool temporal_, bool broken, bool unsupported, uint256 root) internal {
        m.guaranteeItems.push(Item(text, temporal_, broken, unsupported, root));
        m.guaranteeRecs.push(ItemRec(0, false, V_PENDING));
    }
}
// <<< norms-monitor-base
