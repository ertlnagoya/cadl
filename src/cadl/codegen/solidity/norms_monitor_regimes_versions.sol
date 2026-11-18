// >>> norms-monitor-regimes-versions
// Versions together with regime bindings (Appendix K, K.11, "Versions and
// regimes"; Appendix A, A.7.1; Appendix F, F.5 and F.6).
//
// This part is in the monitor generated with the other versions of the
// file when a version of the run has `regimes:` (`cadl codegen -t solidity
// FILE --version OTHER ...`); `cadl codegen` copies it verbatim after the
// base (read with the changes of both the regime part and the versions
// part) and norms_monitor_versions.sol, and the generated contract derives
// from it. The orchestrator sets the current regime with setRegime and the
// version in force with setVersion before the observation at which they
// become current; at an observation the regime becomes current first, then
// the versions take effect. Before the first setRegime the run is in the
// initial regime of the version in force at its first observation, and
// before the first setVersion the first version is in force. A regime that
// is current at some observation must be a regime of every version in
// force at some observation (a version without `regimes:` has only the run
// in no regime). The contract is in force by the regime bindings of the
// version in force. While it is not in force the instance sees nothing and
// its clock stops, as in norms_monitor_regimes.sol; a version that takes
// effect then still reaches the instance (keep, switch, a waiting switch
// judged on the lifecycle state the instance saw last), and the amendment
// is applied at the next observation at which the contract is in force, or
// at which the instance ends, before anything else; a norm that it
// violates there is reported without reparation or transition.
abstract contract CadlNormsMonitorRegimesVersions is CadlNormsMonitorBase {
    /// Only this address may call setRegime and setVersion.
    address public orchestrator;
    /// The current regime ("" for a run in no regime).
    string public regime;
    bool internal regimeSet;
    string[] internal versionNames;
    mapping(bytes32 => uint256) internal versionIndex;      // the index of a version + 1
    mapping(bytes32 => bool) internal isRegime;              // a regime of some version
    mapping(bytes32 => bool) internal regimeOf;              // keccak256(version, regime hash)
    mapping(bytes32 => bool) internal outRegime;             // the same: the contract not in force
    mapping(uint256 => string) internal initialRegime;
    uint256 internal usedVersions;   // the versions in force at some observation, a bit each
    bytes32[] internal seenRegimes;  // the regimes current at some observation
    mapping(bytes32 => bool) internal seenRegime;
    bool internal stepped;           // an observation of the instance has been made
    uint256 internal vk;             // the version in force at the last observation
    int256 internal offset;          // time not in force, before outSince
    int256 internal outSince;        // when the contract left force (when isOut)
    bool internal isOut;
    bool internal held;              // the last observation was left out
    int256 internal traceT;
    bool internal hasTraceT;
    bytes32 internal frozen;         // the lifecycle state the instance saw last

    modifier onlyOrchestrator() {
        require(msg.sender == orchestrator, "only the orchestrator");
        _;
    }

    /// The orchestrator is `orchestrator_`, or the deployer when it is the zero address.
    constructor(address reporter_, address orchestrator_) CadlNormsMonitorBase(reporter_) {
        orchestrator = orchestrator_ == address(0) ? msg.sender : orchestrator_;
    }

    /// The regime that is current from the next observation on (A.7.1).
    function setRegime(string calldata r) external onlyOrchestrator {
        require(isRegime[keccak256(bytes(r))], "'regime' must name a regime of a version of the run");
        regime = r;
        regimeSet = true;
    }

    /// The version in force from the next observation on (K.11). Reverts for
    /// a name that is not a version of the run, and, until the instance has
    /// ended, for a version before the one in force at the last observation.
    function setVersion(string calldata name) external onlyOrchestrator {
        uint256 k = versionIndex[keccak256(bytes(name))];
        require(k > 0, "'version' must name a version of the run");
        require(m.ended || !stepped || k - 1 >= vk,
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

    /// Whether the contract is in force at the last observation.
    function inForce() public view returns (bool) {
        return !isOut;
    }

    /// The time of the last observation (seconds * SCALE); lastT() is the
    /// clock of the contract at the last observation in force.
    function lastTraceT() external view returns (int256) { return traceT; }

    function planVersions() internal override {
        if (m.observations == 0) CadlNormsVersions.begin(m, m.vin);
    }

    function observe(int256 t, ValIn[] calldata values, Msg[] calldata msgs,
                     bool hasState, string calldata state, bool end) external onlyReporter {
        require(t >= -MAX_T && t <= MAX_T, "'t' must be a finite number of at most 2**53 in absolute value");
        require(!hasTraceT || t >= traceT, "time is before the previous observation");
        traceT = t;
        hasTraceT = true;
        if (m.ended) {
            CadlNormsValues.checkObservation(values, msgs);
            m.ignored += 1;
            return;
        }
        (bool inF, int256 c) = enter(t);
        // a started instance follows the versions also while not in force
        if (m.observations > 0) CadlNormsVersions.track(m, m.vin, inF ? keccak256(bytes(m.lc)) : frozen);
        if (inF) {
            held = false;
            observeCore(c, values, msgs, hasState, state, end);
            frozen = keccak256(bytes(m.lc));
            return;
        }
        observeOut(c, values, msgs, hasState, state, end);
    }

    /// The regime and the version of an observation at time t: whether the
    /// contract is in force, and the clock of the contract.
    function enter(int256 t) internal returns (bool inF, int256 c) {
        uint256 k = m.vin;
        if (!stepped) {
            stepped = true;
            if (!regimeSet) regime = initialRegime[k];
        }
        vk = k;
        bytes32 rh = keccak256(bytes(regime));
        checkRun(k, rh);
        inF = !outRegime[keccak256(abi.encode(k, rh))];
        if (inF) {
            if (isOut) { offset += t - outSince; isOut = false; }
            c = t - offset;
        } else {
            if (!isOut) { outSince = t; isOut = true; }
            c = outSince - offset;
        }
        m.shift = t - c;
    }

    /// An observation at which the contract is not in force: the values
    /// wait for the next observation in force; the instance ends here when
    /// the observation ends it.
    function observeOut(int256 c, ValIn[] calldata values, Msg[] calldata msgs,
                        bool hasState, string calldata state, bool end) internal {
        CadlNormsValues.checkObservation(values, msgs);
        uint256 n = m.observations;
        string memory after_ = n == 0 && bytes(m.lc).length == 0 ? m.lcInitial : m.lc;
        if (hasState) after_ = state;
        if (m.data.hasLifecycle) {
            require(m.lcStates[keccak256(bytes(after_))], "the state is not a state of the lifecycle");
        }
        for (uint256 i = 0; i < values.length; i++) {
            bytes32 h = keccak256(bytes(CadlNormsValues.canon(values[i].name)));
            makeKnown(h);
            prune(h);
            m.data.history[h].push(Stored(n, values[i].k, values[i].n, values[i].s));
        }
        if (hasState) m.lc = state;
        if (end || (m.data.hasLifecycle && m.lcTerminal[keccak256(bytes(after_))])) {
            held = false;
            settleOut(c);
        } else {
            held = true;
        }
    }

    /// A regime current at some observation is a regime of every version in
    /// force at some observation (K.11).
    function checkRun(uint256 k, bytes32 rh) internal {
        bool fresh = false;
        if (!seenRegime[rh]) { seenRegime[rh] = true; seenRegimes.push(rh); fresh = true; }
        if (usedVersions >> k & 1 == 0) { usedVersions |= 1 << k; fresh = true; }
        if (!fresh) return;
        for (uint256 v = 0; v < versionNames.length; v++) {
            if (usedVersions >> v & 1 == 0) continue;
            for (uint256 j = 0; j < seenRegimes.length; j++) {
                require(regimeOf[keccak256(abi.encode(v, seenRegimes[j]))],
                        "a regime of a run with versions must be a regime of every version that the run uses (Appendix K, K.11)");
            }
        }
    }

    /// The end of the instance at its last observation; when the contract
    /// was not in force there, on the stopped clock.
    function finish() external onlyReporter {
        if (held && !m.ended) {
            held = false;
            int256 c = outSince - offset;
            m.shift = traceT - c;
            settleOut(c);
            return;
        }
        finishCore();
    }

    /// As in the base; none while the contract is not in force.
    function nextDeadline() external view returns (bool has, int256 due) {
        if (isOut) return (false, 0);
        (has, due) = nextDeadlineCore();
        if (has) due += offset;
    }

    /// The settlement (F.5, item 6) at an observation at which the
    /// contract is not in force: the amendments that waited for it first,
    /// then nothing else of the observation applies.
    function settleOut(int256 c) internal {
        uint256 n = m.observations;
        if (n == 0) CadlNormsVersions.begin(m, m.vin);
        VMarks memory before = CadlNormsVersions.marks(m);
        string memory st = n == 0 && bytes(m.lc).length == 0 ? m.lcInitial : m.lc;
        m.data.obsT.push(c);
        m.data.obsNow.push(c + m.shift);
        m.data.obsSt.push(st);
        m.observations = n + 1;
        m.lastT = c;
        m.hasLastT = true;
        if (n == 0) {
            for (uint256 i = 0; i < m.recs.length; i++) {
                NormDef storage d = m.normDefs[m.dp[i]];
                if (d.trigger < 0 && !d.target) setSt(m, i, ACTIVE, n, c);
            }
        } else {
            CadlNormsVersions.amendPath(m, n, c);
        }
        // the norms that the amendment violated: reported, without
        // reparation or transition (the instance ends here)
        uint256[] memory v = vfirst(m, new uint256[](0));
        for (uint256 i = 0; i < v.length; i++) onViolation(v[i], n, c, false);
        settle(n, c);
        finishItems(true);
        CadlNormsVersions.changes(m, before, c);
    }

    // --- filling the model (used by the constructor of the generated contract) ----------

    /// The definitions that follow are those of version `name` (see
    /// CadlNormsMonitorVersions._version); `initial` is its initial regime
    /// ("" for a version without regimes:).
    function _version(string memory name, uint8 rule, string memory initial) internal {
        uint256 v = versionNames.length;
        versionIndex[keccak256(bytes(name))] = v + 1;
        versionNames.push(name);
        m.vrule[v] = rule;
        initialRegime[v] = initial;
        if (bytes(initial).length == 0) regimeOf[keccak256(abi.encode(v, keccak256("")))] = true;
        _versionBounds();
    }

    /// A regime of the last version; `out`: the contract is not in force in it.
    function _versionRegime(string memory name, bool out) internal {
        uint256 v = versionNames.length - 1;
        bytes32 h = keccak256(bytes(name));
        isRegime[h] = true;
        regimeOf[keccak256(abi.encode(v, h))] = true;
        outRegime[keccak256(abi.encode(v, h))] = out;
    }

    function _versionState(string memory state) internal {
        m.vstate[keccak256(abi.encode(versionNames.length - 1, keccak256(bytes(state))))] = true;
    }

    function _versionsEnd() internal {
        _versionBounds();
    }

    function _versionBounds() private {
        m.vnlo.push(m.normDefs.length);
        m.valo.push(m.assumeItems.length);
        m.vglo.push(m.guaranteeItems.length);
        m.vrlo.push(m.rules.length);
    }

    function _onAmendment(uint8 how, string memory target) internal {
        NormDef storage d = m.normDefs[m.normDefs.length - 1];
        d.onAm = how;
        d.transformTo = target;
    }

    function _shareContent(uint256 j) internal {
        uint256 i = m.normDefs.length - 1;
        m.sl[i] = m.sl[j];
        m.normDefs[i].tmode = m.normDefs[j].tmode;
    }
}
// <<< norms-monitor-regimes-versions
