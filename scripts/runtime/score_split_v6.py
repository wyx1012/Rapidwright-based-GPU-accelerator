"""Use live geometry scores only for groups already split by GPU memory."""
RESOURCE_BOUNDARY_BYTES = 192 << 20
MAX_FEATURE_SCORE = (1 << 31) - 1


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("Unexpected score-split anchor: " + old.splitlines()[0])
    return source.replace(old, new, 1)


def patch_broker(source):
    anchor = '            device.stdin.write(f"route {batch} {len(members)}\\n")'
    replacement = f"""            if feature["resource_input_bytes"] >= {RESOURCE_BOUNDARY_BYTES} and len(members) > 1:
                scores = [member["feature_score"] for member in members]
                check(all(type(score) is int and 0 <= score <= {MAX_FEATURE_SCORE} for score in scores),
                      "Invalid live GPU feature score")
                score_tokens = " ".join(map(str, scores))
                device.stdin.write(f"route_scored {{batch}} {{len(members)}} {{score_tokens}}\\n")
            else:
                device.stdin.write(f"route {{batch}} {{len(members)}}\\n")"""
    return replace_once(source, anchor, replacement)


def patch_cuda(source):
    request = """        U32 members;
        need(operation == "route" && bool(std::cin >> count >> members) && count == phase
                && members >= 1 && members <= channels, "Unexpected native batch request");"""
    scored = """        U32 members;
        const bool scored = operation == "route_scored";
        need((operation == "route" || scored) && bool(std::cin >> count >> members) && count == phase
                && members >= 1 && members <= channels, "Unexpected native batch request");
        U32 featureScores[MaxChannels]{};
        if (scored) {
            for (U32 member = 0; member < members; ++member) {
                need(bool(std::cin >> featureScores[member]) && featureScores[member] <= 0x7fffffffu,
                     "Invalid live feature score");
            }
        }"""
    source = replace_once(source, request, scored)
    source = replace_once(source, "        U32 groups = 0, retries = 0, maxLaunched = 0, maxCompleted = 0, mask = 0;",
                          "        U32 groups = 0, retries = 0, maxLaunched = 0, maxCompleted = 0, mask = 0;\n"
                          "        U32 scoredGroups = 0;")
    baseline = """            const std::vector<U32> group(remaining.begin(), remaining.begin() + width);
            remaining.erase(remaining.begin(), remaining.begin() + width);"""
    balanced = """            std::vector<U32> group(remaining.begin(), remaining.begin() + width);
            bool appliedScores = false;
            if (scored && width > 1 && remaining.size() > width) {
                // The allocator already requires a split. Estimate each group's work
                // using its member score sum, preserving the existing width of every group.
                const size_t parts = (remaining.size() + width - 1) / width;
                std::vector<U32> ordered = remaining;
                std::sort(ordered.begin(), ordered.end(), [&](U32 left, U32 right) {
                    if (featureScores[left] != featureScores[right]) {
                        return featureScores[left] > featureScores[right];
                    }
                    return left < right;
                });
                U64 originalPeak = 0;
                for (size_t part = 0; part < parts; ++part) {
                    U64 load = 0;
                    const size_t end = std::min(remaining.size(), (part + 1) * width);
                    for (size_t index = part * width; index < end; ++index) {
                        load += featureScores[remaining[index]];
                    }
                    originalPeak = std::max(originalPeak, load);
                }
                std::vector<U64> loads(parts, 0);
                std::vector<U32> filled(parts, 0);
                std::vector<U32> balanced;
                balanced.reserve(width);
                for (const U32 member : ordered) {
                    size_t selected = parts;
                    for (size_t part = 0; part < parts; ++part) {
                        const size_t capacity = std::min<size_t>(width, remaining.size() - part * width);
                        if (filled[part] < capacity && (selected == parts || loads[part] < loads[selected])) {
                            selected = part;
                        }
                    }
                    need(selected != parts, "Unable to balance frozen GPU split");
                    ++filled[selected];
                    loads[selected] += featureScores[member];
                    if (selected == 0) balanced.push_back(member);
                }
                if (*std::max_element(loads.begin(), loads.end()) < originalPeak) {
                    std::sort(balanced.begin(), balanced.end());
                    appliedScores = balanced != group;
                    if (appliedScores) {
                        group.swap(balanced);
                        ++scoredGroups;
                    }
                }
            }
            if (appliedScores) {
                for (const U32 member : group) {
                    remaining.erase(std::find(remaining.begin(), remaining.end(), member));
                }
            } else {
                remaining.erase(remaining.begin(), remaining.begin() + width);
            }"""
    source = replace_once(source, baseline, balanced)
    receipt = '                    << ",\\"resident_maps\\":" << discovered.size()'
    updated = ('                    << ",\\"score_balanced_groups\\":" << scoredGroups\n'
               '                    << ",\\"resident_maps\\":" << discovered.size()')
    return replace_once(source, receipt, updated)
