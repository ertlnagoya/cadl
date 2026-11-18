// >>> norms-monitor-regimes
// Regimes (Appendix A, A.7.1; Appendix F, F.5, "Contracts not in force").
//
// This part is in the monitor of a contract that some regime of the file
// does not have in force; `cadl codegen -t solidity` copies it verbatim
// after the base, whose observe, finish and nextDeadline it replaces. The
// orchestrator (the deployer unless the constructor names another address)
// sets the current regime with setRegime before the observation at which
// it becomes current, as the key "regime" of a trace; before the first
// setRegime the run is in the initial regime. At an observation of a
// regime that does not have the contract in force the instance sees
// nothing: no change, no event, its values are seen from the next
// observation in force, its messages are not seen, and the clock of the
// contract stops. Deadlines and the intervals of the temporal operators run
// on that clock (the time of the trace without the time not in force);
// `now` and the times of the events are times of the trace. An instance
// that ends at such an observation (end, a terminal state, or finish()
// after it) is settled there, on the stopped clock.
abstract contract CadlNormsMonitorRegimes is CadlNormsMonitorBase {
    mapping(bytes32 => bool) internal isRegime;
    mapping(bytes32 => bool) internal outRegime;
    /// The current regime.
    string public regime;
    /// Only this address may call setRegime.
    address public orchestrator;
    int256 internal offset;          // time not in force, before outSince
    int256 internal outSince;        // when the contract left force (when isOut)
    bool internal isOut;
    bool internal held;              // the last observation was left out
    int256 internal traceT;
    bool internal hasTraceT;

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
        require(isRegime[keccak256(bytes(r))], "'regime' must name a regime of the file");
        regime = r;
    }

    /// Whether the current regime has the contract in force.
    function inForce() public view returns (bool) {
        return !outRegime[keccak256(bytes(regime))];
    }

    /// The time of the last observation (seconds * SCALE); lastT() is the
    /// clock of the contract at the last observation in force.
    function lastTraceT() external view returns (int256) { return traceT; }

    function observe(int256 t, ValIn[] calldata values, Msg[] calldata msgs,
                     bool hasState, string calldata state, bool end) external onlyReporter {
        require(t >= -MAX_T && t <= MAX_T, "'t' must be a finite number of at most 2**53 in absolute value");
        require(!hasTraceT || t >= traceT, "time is before the previous observation");
        traceT = t;
        hasTraceT = true;
        int256 c;
        if (inForce()) {
            if (isOut) { offset += t - outSince; isOut = false; }
            c = t - offset;
        } else {
            if (!isOut) { outSince = t; isOut = true; }
            c = outSince - offset;
        }
        m.shift = t - c;
        if (m.ended || !isOut) {
            held = false;
            observeCore(c, values, msgs, hasState, state, end);
            return;
        }
        // not in force: the values wait for the next observation in force
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
    /// contract is not in force: nothing else of the observation applies.
    function settleOut(int256 c) internal {
        (uint8[] memory ns, uint8[] memory nv, uint8[] memory am, uint8[] memory gm) = marks();
        uint256 n = m.observations;
        string memory before = n == 0 && bytes(m.lc).length == 0 ? m.lcInitial : m.lc;
        m.data.obsT.push(c);
        m.data.obsNow.push(c + m.shift);
        m.data.obsSt.push(before);
        m.observations = n + 1;
        m.lastT = c;
        m.hasLastT = true;
        if (n == 0) {
            for (uint256 i = 0; i < m.normDefs.length; i++) {
                NormDef storage d = m.normDefs[i];
                if (d.trigger < 0 && !d.target) setSt(m, i, ACTIVE, n, c);
            }
        }
        settle(n, c);
        finishItems(true);
        emitChanges(ns, nv, am, gm, c);
    }

    function _regime(string memory name, bool out) internal {
        isRegime[keccak256(bytes(name))] = true;
        outRegime[keccak256(bytes(name))] = out;
    }
}
// <<< norms-monitor-regimes
