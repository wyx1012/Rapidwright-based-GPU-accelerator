"""Enable exact resident-CSR search for bounded same-SLR requests selected for GPU."""


_PARITY_FIELDS = """
bool equalSameSlrPath(const std::string& active, const std::string& legacy) {
    for (const std::string field : {"\\nCHANGED", "\\nPATH", "\\nPARTIAL", "\\nINGRESS"}) {
        const auto a = active.find(field), b = legacy.find(field);
        if (a == std::string::npos || b == std::string::npos) return false;
        const auto endA = active.find('\\n', a + 1), endB = legacy.find('\\n', b + 1);
        if (endA == std::string::npos || endB == std::string::npos
            || active.compare(a, endA - a, legacy, b, endB - b) != 0)
            return false;
    }
    return true;
}

"""


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise RuntimeError(f"Unexpected v9d CUDA anchor: {old.splitlines()[0]}")
    return source.replace(old, new, 1)


def patch(source):
    """Patch the materialized v9d hybrid.cu, reusing the existing response ABI."""
    source = replace_once(
        source,
        """U32* labels, U32* activity, U32 channel, SearchWork& work, std::string& answer) {""",
        """U32* labels, U32* activity, U32 channel, SearchWork& work, std::string& answer,
                       bool narrowed) {""",
    )
    source = replace_once(
        source,
        """    if (!enabled || mode != (Target | Dominance | Bloom | Stale) || !activeFrontierKeys
        || !attempt.cross || attempt.box != 1 || attempt.north == attempt.south""",
        """    static const bool sameSlrEnabled = [] {
        const char* value = std::getenv("FIDES_GPU_SAME_SLR_FRONTIER_V11");
        return value && std::strcmp(value, "1") == 0;
    }();
    if (!enabled || mode != (Target | Dominance | Bloom | Stale) || !activeFrontierKeys
        || attempt.box != 1
        || (attempt.cross ? !narrowed || attempt.north == attempt.south : !sameSlrEnabled)""",
    )
    source = replace_once(source, "std::string solveAttempt(const Attempt& attempt",
                          _PARITY_FIELDS + "std::string solveAttempt(const Attempt& attempt")
    source = replace_once(source, "bool narrowed = false) {",
                          "bool narrowed = false, bool legacyOnly = false) {")
    source = replace_once(
        source,
        """    if (narrowed && tryActiveFrontier(attempt, data, mode, access, usage, discovered, channelActivity, channel,
                          work, activeFrontierReply))
        return activeFrontierReply;""",
        """    if (!legacyOnly && tryActiveFrontier(attempt, data, mode, access, usage, discovered, channelActivity,
                                          channel, work, activeFrontierReply, narrowed)) {
        static const bool requireSameSlrParity = [] {
            const char* value = std::getenv("FIDES_GPU_SAME_SLR_PARITY_V11");
            return value && std::strcmp(value, "1") == 0;
        }();
        if (!attempt.cross && requireSameSlrParity) {
            static std::once_flag sampled;
            static bool equalPaths = false;
            std::call_once(sampled, [&] {
                SearchWork legacyWork;
                const auto start = std::chrono::steady_clock::now();
                try {
                    const auto legacy = solveAttempt(attempt, data, mode, access, usage, discovered, nullptr,
                                                     channel, legacyWork, narrowed, true);
                    const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
                            std::chrono::steady_clock::now() - start).count();
                    equalPaths = equalSameSlrPath(activeFrontierReply, legacy);
                    if (equalPaths) {
                        std::ostringstream receipt;
                        receipt << "SAME_SLR_PARITY 1 " << elapsed << ' ' << legacyWork.candidates << '\\n';
                        activeFrontierReply += receipt.str();
                        std::cerr << receipt.str();
                    }
                } catch (const NoPath&) {
                    // Do not let a same-scope parity difference pass as an ordinary corridor retry.
                    equalPaths = false;
                }
            });
            need(equalPaths, "Same-SLR resident search disagrees with matched legacy GPU path");
        }
        return activeFrontierReply;
    }""",
    )
    # Let the original GPU implementation establish no-path for the newly admitted
    # class, on the exact same attempt before the existing corridor retry.
    return replace_once(
        source,
        """        if (!search.counts.failure && search.targetCost != Inf
            && !activeFrontierTrace(data, access, usage, attempt, weights, labels, search.counts.touched, path)) {""",
        """        if (!search.counts.failure && !attempt.cross && search.targetCost == Inf) {
            search.counts.failure = ActiveSameSlrNoPath;
        }
        if (!search.counts.failure && search.targetCost != Inf
            && !activeFrontierTrace(data, access, usage, attempt, weights, labels, search.counts.touched, path)) {""",
    )


def patch_frontier(source):
    """Patch the paired v9d active_frontier_v9.cuh while leaving its trace unchanged."""
    source = replace_once(
        source,
        """    ActiveScratchPressure
};""",
        """    ActiveScratchPressure, ActiveSameSlrNoPath
};""",
    )
    source = replace_once(
        source,
        """    if (access.type[child] == access.ord[SLL]) {
        U32 canonical""",
        """    if (access.type[child] == access.ord[SLL]) {
        // Same-SLR access should exclude SLL. If the device cannot establish
        // that, keep the original ingress-aware GPU route for this attempt.
        if (!attempt.cross) return {state, {}, false};
        U32 canonical""",
    )
    return replace_once(
        source,
        """    if (access.type[attempt.source] == access.ord[SLL] || access.type[attempt.sink] == access.ord[SLL]) {""",
        """    bool uncertainSameSlr = false;
    if (!attempt.cross && access.slr[0]) {
        const int sourceSlr = slrIndex(access, attempt.source, Unseen);
        uncertainSameSlr = sourceSlr < 0 || sourceSlr != slrIndex(access, attempt.sink, Unseen);
    }
    if (uncertainSameSlr || access.type[attempt.source] == access.ord[SLL]
        || access.type[attempt.sink] == access.ord[SLL]) {""",
    )
