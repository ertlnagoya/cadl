// >>> norms-monitor-versions
// Versions of a run (Appendix K, K.11; Appendix F, F.6).
//
// This part is in the monitor generated with the other versions of the
// file (`cadl codegen -t solidity FILE --version OTHER ...`); `cadl codegen`
// copies it verbatim after the base, which it reads with the changes listed
// in norms_monitor_sol.py (_versions_base). The constructor fills the
// definitions of the contract in every version, in the order of the
// sequence by previous:, one after the other; the instance has its own list
// of norm instances (Monitor.recs), each with the definition it follows
// (Monitor.dp), and its own list of assume: and guarantee: items, each with
// its definition, its start and its identity.
//
// The orchestrator (the deployer unless the constructor names another
// address) sets the version in force with setVersion before the
// observation at which it takes effect, as the key "version" of a trace;
// before the first setVersion the first version is in force. Computing
// when a version takes effect (from the effective times, K.11) stays with
// the orchestrator. The instance follows the version in force at its first
// observation. When a version takes effect, the instance follows the
// transitional rule of its contract in that version: keep leaves it as it
// is, switch amends it there (F.6), and switch with in_states amends it at
// the first observation whose lifecycle state before it is one of them; a
// waiting switch whose state holds is made first, and a waiting switch is
// dropped when a later version takes effect (norms_eval.VersionTracker).
//
// The amendment is the one of F.6 (norms_eval._Engine._amend): a norm
// instance in a final state is kept as it is (with the definition it has),
// an inactive one takes the new definition or is removed, one in force is
// discharged (on_amendment discharge or transform, a norm the new version
// does not have, a kind that changes) or carried over with its state and
// the time elapsed against its deadline. A carried instance keeps its
// pending candidates when its content is the same text; its window has
// closed under the new deadline when it has none, and it is then violated
// (an obligation, reported first in step 5) or fulfilled (a prohibition).
// The temporal content of a norm keeps its candidates in a slot (Monitor.sl)
// that the definitions of the norm with the same content share across the
// versions. Then a transform activates its target with the elapsed time, a
// new norm without trigger is activated, and an item with the same text
// continues; an item the new version does not have is closed when it is not
// decided. Norms and items that the new version does not have stay in the
// lists after those of the contract. NormChanged reports a norm that is new
// in the version with the previous state and verdict 255 (none), and one
// that the version removes with the state and verdict 255.

/// The norm of the instance with the id whose keccak256 is h, or -1.
function vfind(Monitor storage m, bytes32 h) view returns (int256) {
    for (uint256 i = 0; i < m.recs.length; i++) if (m.idh[m.dp[i]] == h) return int256(i);
    return -1;
}

/// The norms violated by the amendments of this observation (F.6), then
/// those of step 4.
function vfirst(Monitor storage m, uint256[] memory later) returns (uint256[] memory out) {
    uint256 c = m.amendViolated.length;
    if (c == 0) return later;
    out = new uint256[](c + later.length);
    for (uint256 i = 0; i < c; i++) out[i] = uint256(vfind(m, m.amendViolated[i]));
    for (uint256 i = 0; i < later.length; i++) out[c + i] = later[i];
    delete m.amendViolated;
}

// The marks of the instance before an observation (norms by id, items by identity).
struct VMarks { bytes32[] ids; uint256[] defs; uint8[] ns; uint8[] nv; uint256[] au; uint8[] am; uint256[] gu; uint8[] gm; }

library CadlNormsVersions {
    event NormChanged(uint256 index, string id, uint8 state, uint8 verdict, uint8 previousState, uint8 previousVerdict, int256 t);
    event ItemChanged(bool guarantee, uint256 index, uint8 mark, uint8 previous, int256 t);

    // the transitional rule of the contract in a version
    uint8 internal constant R_KEEP = 0;
    uint8 internal constant R_SWITCH = 1;
    uint8 internal constant R_IN_STATES = 2;
    // on_amendment of a norm
    uint8 internal constant A_CARRY = 0;
    uint8 internal constant A_DISCHARGE = 1;
    uint8 internal constant A_TRANSFORM = 2;

    /// Before the observation is recorded: the version the instance starts
    /// with (the first observation), or the versions it is amended to at
    /// this observation, from the lifecycle state before it.
    function plan(Monitor storage m) public {
        if (m.observations == 0) {
            start(m, m.vin);
            return;
        }
        step(m, m.vin, keccak256(bytes(m.lc)));
    }

    /// The instance starts under version k.
    function begin(Monitor storage m, uint256 k) public {
        start(m, k);
    }

    /// Version k is in force at an observation of a started instance whose
    /// lifecycle state (as the instance sees it) has the hash h: the
    /// versions it is amended to are added to vpath (norms_eval.VersionTracker).
    function track(Monitor storage m, uint256 k, bytes32 h) public {
        step(m, k, h);
    }

    function step(Monitor storage m, uint256 k, bytes32 h) internal {
        if (m.vpendSet && inStates(m, m.vpend, h)) {
            m.vpath.push(m.vpend);
            m.vfol = m.vpend;
            m.vpendSet = false;
        }
        for (uint256 v = m.vcur + 1; v <= k; v++) {
            m.vpendSet = false;           // a waiting switch is dropped
            uint8 rule = m.vrule[v];
            if (rule == R_SWITCH || (rule == R_IN_STATES && inStates(m, v, h))) {
                m.vpath.push(v);
                m.vfol = v;
            } else if (rule == R_IN_STATES) {
                m.vpend = v;
                m.vpendSet = true;
            }
        }
        m.vcur = k;
    }

    function marks(Monitor storage m) public view returns (VMarks memory b) {
        uint256 c = m.recs.length;
        b.ids = new bytes32[](c);
        b.defs = new uint256[](c);
        b.ns = new uint8[](c);
        b.nv = new uint8[](c);
        for (uint256 i = 0; i < c; i++) {
            b.defs[i] = m.dp[i];
            b.ids[i] = m.idh[b.defs[i]];
            b.ns[i] = m.recs[i].state;
            b.nv[i] = m.recs[i].verdict;
        }
        (b.au, b.am) = itemMarks(m.assumeRecs, false);
        (b.gu, b.gm) = itemMarks(m.guaranteeRecs, true);
    }

    function itemMark(ItemRec storage r, bool guarantee) internal view returns (uint8) {
        if (guarantee) return r.verdict;
        return r.value == 1 ? 1 : r.value == 2 ? 2 : 0;
    }

    function itemMarks(ItemRec[] storage rs, bool guarantee) internal view returns (uint256[] memory u, uint8[] memory k) {
        u = new uint256[](rs.length);
        k = new uint8[](rs.length);
        for (uint256 i = 0; i < rs.length; i++) { u[i] = rs[i].uid; k[i] = itemMark(rs[i], guarantee); }
    }

    /// The changes since `b`: a norm by its id (255: none, for a norm new in
    /// the version or removed by it), an item by its identity.
    function changes(Monitor storage m, VMarks memory b, int256 t) public {
        bool[] memory seen = new bool[](b.ids.length);
        for (uint256 i = 0; i < m.recs.length; i++) {
            bytes32 h = m.idh[m.dp[i]];
            uint8 ps = 255;
            uint8 pv = 255;
            for (uint256 k = 0; k < b.ids.length; k++) {
                if (b.ids[k] == h) { ps = b.ns[k]; pv = b.nv[k]; seen[k] = true; break; }
            }
            NormRec storage r = m.recs[i];
            if (r.state != ps || r.verdict != pv) emit NormChanged(i, m.normDefs[m.dp[i]].id, r.state, r.verdict, ps, pv, t);
        }
        for (uint256 k = 0; k < b.ids.length; k++) {
            if (!seen[k]) emit NormChanged(k, m.normDefs[b.defs[k]].id, 255, 255, b.ns[k], b.nv[k], t);
        }
        itemChanges(false, m.assumeRecs, b.au, b.am, t);
        itemChanges(true, m.guaranteeRecs, b.gu, b.gm, t);
    }

    function itemChanges(bool guarantee, ItemRec[] storage rs, uint256[] memory u, uint8[] memory mk, int256 t) internal {
        for (uint256 i = 0; i < rs.length; i++) {
            uint8 now_ = itemMark(rs[i], guarantee);
            uint8 prev = 0;
            for (uint256 k = 0; k < u.length; k++) if (u[k] == rs[i].uid) { prev = mk[k]; break; }
            if (now_ != prev) emit ItemChanged(guarantee, i + 1, now_, prev, t);
        }
    }

    function inStates(Monitor storage m, uint256 v, bytes32 state) internal view returns (bool) {
        return m.vstate[keccak256(abi.encode(v, state))];
    }

    /// The instance of version k: its norms, items and rules.
    function start(Monitor storage m, uint256 k) internal {
        m.vcur = k;
        m.vfol = k;
        for (uint256 q = m.vnlo[k]; q < m.vnlo[k + 1]; q++) {
            m.dp[m.recs.length] = q;
            m.recs.push().act = -1;
        }
        for (uint256 q = m.valo[k]; q < m.valo[k + 1]; q++) {
            m.assumeRecs.push(ItemRec(0, false, V_PENDING, false, 0, q, m.uid++));
        }
        for (uint256 q = m.vglo[k]; q < m.vglo[k + 1]; q++) {
            m.guaranteeRecs.push(ItemRec(0, false, V_PENDING, false, 0, q, m.uid++));
        }
        m.rlo = m.vrlo[k];
        m.rhi = m.vrlo[k + 1];
    }

    /// The amendments of observation n (F.6, before step 1), to the
    /// versions that plan() or track() found, in order.
    function amendPath(Monitor storage m, uint256 n, int256 t) public {
        for (uint256 i = 0; i < m.vpath.length; i++) amend(m, m.vpath[i], n, t);
        delete m.vpath;
    }

    // the new lists of an amendment
    struct Lists {
        uint256 lb; uint256 cb;                          // the definitions of the new version
        NormRec[] nr; uint256[] nd; bool[] have;         // its norms, from the old instances
        NormRec[] rr; uint256[] rd; uint256 nret;        // the norms kept after them
        uint256[] tfd; int256[] tfe; uint256 ntf;        // the transforms: definition, elapsed time
    }

    /// The amendment of the instance to version b at observation n.
    function amend(Monitor storage m, uint256 b, uint256 n, int256 t) internal {
        Lists memory x;
        x.lb = m.vnlo[b];
        x.cb = m.vnlo[b + 1] - x.lb;
        uint256 old = m.recs.length;
        x.nr = new NormRec[](x.cb);
        x.nd = new uint256[](x.cb);
        x.have = new bool[](x.cb);
        x.rr = new NormRec[](old);
        x.rd = new uint256[](old);
        x.tfd = new uint256[](old);
        x.tfe = new int256[](old);
        for (uint256 o = 0; o < old; o++) instance(m, x, b, o, n, t);
        while (m.recs.length > x.cb + x.nret) m.recs.pop();
        while (m.recs.length < x.cb + x.nret) m.recs.push();
        for (uint256 j = 0; j < x.cb; j++) {
            if (x.have[j]) {
                m.recs[j] = x.nr[j];
                m.dp[j] = x.nd[j];
            } else {                                       // new in the version
                m.recs[j] = NormRec(INACTIVE, V_PENDING, false, 0, -1, false, 0, 0, 0);
                m.dp[j] = x.lb + j;
            }
        }
        for (uint256 k = 0; k < x.nret; k++) {
            m.recs[x.cb + k] = x.rr[k];
            m.dp[x.cb + k] = x.rd[k];
        }
        // the transforms, then the new norms without trigger
        for (uint256 k = 0; k < x.ntf; k++) {
            uint256 q = m.vdef[keccak256(abi.encode(b, keccak256(bytes(m.normDefs[x.tfd[k]].transformTo))))];
            if (q == 0 || m.recs[q - 1 - x.lb].state != INACTIVE) continue;
            uint256 w = q - 1 - x.lb;
            uint8 kind = m.normDefs[m.dp[w]].kind;
            m.recs[w].before = kind == OBLIGATION || kind == PROHIBITION ? x.tfe[k] : int256(0);
            setSt(m, w, ACTIVE, n, t);
        }
        for (uint256 j = 0; j < x.cb; j++) {
            if (x.have[j]) continue;
            NormDef storage d = m.normDefs[x.lb + j];
            if (m.recs[j].state == INACTIVE && d.trigger < 0 && !d.target) setSt(m, j, ACTIVE, n, t);
        }
        amendItems(m, m.assumeRecs, m.assumeItems, m.valo[b], m.valo[b + 1], n);
        amendItems(m, m.guaranteeRecs, m.guaranteeItems, m.vglo[b], m.vglo[b + 1], n);
        m.rlo = m.vrlo[b];
        m.rhi = m.vrlo[b + 1];
    }

    /// Old norm instance o: kept, discharged, carried over or removed.
    function instance(Monitor storage m, Lists memory x, uint256 b, uint256 o, uint256 n, int256 t) internal {
        uint256 def = m.dp[o];
        // the definition with this id in b, plus one
        uint256 q = m.vdef[keccak256(abi.encode(b, m.idh[def]))];
        uint8 st = m.recs[o].state;
        if (st == INACTIVE) {
            if (q == 0) return;                        // removed
            delete m.recs[o];                          // a new instance of the new definition
            m.recs[o].act = -1;
            def = q - 1;
        } else if (st < FULFILLED) {
            NormDef storage od = m.normDefs[def];
            if (od.onAm == A_TRANSFORM) {
                x.tfd[x.ntf] = def;
                x.tfe[x.ntf++] = elapsed(m, o, t);
                setSt(m, o, DISCHARGED, n, t);
            } else if (od.onAm == A_DISCHARGE || q == 0 || m.normDefs[q - 1].kind != od.kind) {
                setSt(m, o, DISCHARGED, n, t);
            } else {
                carry(m, o, def, q - 1, n, t);
                def = q - 1;
            }
        }
        if (q != 0) {
            x.nr[q - 1 - x.lb] = m.recs[o];
            x.nd[q - 1 - x.lb] = def;
            x.have[q - 1 - x.lb] = true;
        } else {
            x.rr[x.nret] = m.recs[o];
            x.rd[x.nret++] = def;
        }
    }

    /// Norm instance o, in force, continued under definition nd: its state
    /// and elapsed time are kept; its candidates when the content is the
    /// same (the same slot). A window that the new deadline has closed
    /// without a pending candidate decides it now.
    function carry(Monitor storage m, uint256 o, uint256 od, uint256 nd, uint256 n, int256 t) internal {
        uint256 s = m.sl[nd];
        bool same = m.sl[od] == s;
        if (!same) {
            CadlNormsWindows.clear(m.windows[m.sl[od]]);
            CadlNormsMulti.clear(m.multis[m.sl[od]]);
            if (m.hasGeneral) CadlNormsMulti.gclear(m.generals[m.sl[od]]);
        }
        NormDef storage d = m.normDefs[nd];
        if ((d.kind != OBLIGATION && d.kind != PROHIBITION) || !d.hasDeadline
            || elapsed(m, o, t) <= d.deadline) return;
        if (same && (CadlNormsWindows.pending(m.windows[s]) || m.multis[s].pend
                     || (m.hasGeneral && m.generals[s].pend))) return;
        if (d.kind == OBLIGATION) {
            setSt(m, o, VIOLATED, n, t);
            m.amendViolated.push(m.idh[nd]);
        } else {
            setSt(m, o, FULFILLED, n, t);
        }
    }

    /// The items of version b (norms_eval._Engine._amend_items): an item
    /// continues an open one with the same text, the others start at n; an
    /// old item left over is closed when it is not decided, and kept at the end.
    function amendItems(Monitor storage m, ItemRec[] storage recs, Item[] storage defs, uint256 lo,
                        uint256 hi, uint256 n) internal {
        uint256 old = recs.length;
        ItemRec[] memory o = new ItemRec[](old);
        bytes32[] memory oh = new bytes32[](old);
        for (uint256 i = 0; i < old; i++) {
            o[i] = recs[i];
            oh[i] = keccak256(bytes(defs[o[i].def].text));
        }
        bool[] memory taken = new bool[](old);
        ItemRec[] memory out = new ItemRec[](hi - lo + old);
        uint256 c = 0;
        for (uint256 q = lo; q < hi; q++) {
            uint256 f = open(o, oh, taken, keccak256(bytes(defs[q].text)));
            out[c++] = f < old ? o[f] : ItemRec(0, false, V_PENDING, false, n, q, m.uid++);
        }
        for (uint256 i = 0; i < old; i++) {
            if (taken[i]) continue;
            if (!o[i].closed && !defs[o[i].def].broken && (o[i].value == 0 || o[i].value == 3)) {
                o[i].closed = true;
                o[i].value = 0;
            }
            out[c++] = o[i];
        }
        while (recs.length > c) recs.pop();
        while (recs.length < c) recs.push();
        for (uint256 i = 0; i < c; i++) recs[i] = out[i];
    }

    /// The first open old item with text h that is not taken (taking it), or o.length.
    function open(ItemRec[] memory o, bytes32[] memory oh, bool[] memory taken, bytes32 h)
            internal pure returns (uint256) {
        for (uint256 i = 0; i < o.length; i++) {
            if (!taken[i] && !o[i].closed && oh[i] == h) { taken[i] = true; return i; }
        }
        return o.length;
    }
}

abstract contract CadlNormsMonitorVersions is CadlNormsMonitorBase {
    /// Only this address may call setVersion.
    address public orchestrator;
    string[] internal versionNames;
    mapping(bytes32 => uint256) internal versionIndex;      // the index of a version + 1

    modifier onlyOrchestrator() {
        require(msg.sender == orchestrator, "only the orchestrator");
        _;
    }

    /// The orchestrator is `orchestrator_`, or the deployer when it is the zero address.
    constructor(address reporter_, address orchestrator_) CadlNormsMonitorBase(reporter_) {
        orchestrator = orchestrator_ == address(0) ? msg.sender : orchestrator_;
    }

    function planVersions() internal override {
        CadlNormsVersions.plan(m);
    }

    /// The version in force from the next observation on (K.11). Reverts for
    /// a name that is not a version of the run, and, until the instance has
    /// ended, for a version before the one in force at the last observation.
    function setVersion(string calldata name) external onlyOrchestrator {
        uint256 k = versionIndex[keccak256(bytes(name))];
        require(k > 0, "'version' must name a version of the run");
        require(m.ended || m.observations == 0 || k - 1 >= m.vcur,
                "the version comes before the version in force (Appendix K, K.11)");
        m.vin = k - 1;
    }

    /// The versions of the run, in the order of the sequence by previous:.
    function versions() external view returns (string[] memory) { return versionNames; }

    /// The version in force, and the version whose contract the instance
    /// follows (the one in force before the first observation).
    function version() external view returns (string memory inForce, string memory follows) {
        inForce = versionNames[m.vin];
        follows = versionNames[m.observations == 0 ? m.vin : m.vfol];
    }

    // --- filling the model (used by the constructor of the generated contract) ----------

    /// The definitions that follow are those of version `name`, whose
    /// transitional rule for the contract is `rule` (0 keep, 1 switch, 2
    /// switch with in_states: the calls of _versionState add them).
    function _version(string memory name, uint8 rule) internal {
        uint256 v = versionNames.length;
        versionIndex[keccak256(bytes(name))] = v + 1;
        versionNames.push(name);
        m.vrule[v] = rule;
        _versionBounds();
    }

    function _versionState(string memory state) internal {
        m.vstate[keccak256(abi.encode(versionNames.length - 1, keccak256(bytes(state))))] = true;
    }

    /// After the last version.
    function _versionsEnd() internal {
        _versionBounds();
    }

    function _versionBounds() private {
        m.vnlo.push(m.normDefs.length);
        m.valo.push(m.assumeItems.length);
        m.vglo.push(m.guaranteeItems.length);
        m.vrlo.push(m.rules.length);
    }

    /// on_amendment of the last norm: 1 discharge, 2 transform to `target`.
    function _onAmendment(uint8 how, string memory target) internal {
        NormDef storage d = m.normDefs[m.normDefs.length - 1];
        d.onAm = how;
        d.transformTo = target;
    }

    /// The temporal content of the last norm is that of definition j (the
    /// same norm, with the same content, in an earlier version): they share
    /// its candidates.
    function _shareContent(uint256 j) internal {
        uint256 i = m.normDefs.length - 1;
        m.sl[i] = m.sl[j];
        m.normDefs[i].tmode = m.normDefs[j].tmode;
    }
}
// <<< norms-monitor-versions
