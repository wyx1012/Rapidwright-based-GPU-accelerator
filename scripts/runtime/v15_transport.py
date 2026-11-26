"""Ship one compact attempt per lane with just four optional corridor coordinates."""
from v11_window import replace_once as once


def cuda(src):
    src = once(src, """    const Attempt original = readAttempt(originalFile);
    const auto stem""", """    std::string attemptLine;
    need(bool(std::getline(originalFile, attemptLine)), "Missing original search bounds");
    std::istringstream originalStream(attemptLine);
    const Attempt original = readAttempt(originalStream);
    unsigned hasCorridor;
    need(bool(originalFile >> hasCorridor) && hasCorridor <= 1, "Invalid corridor marker");
    Attempt narrow = original;
    if (hasCorridor) {
        need(bool(originalFile >> narrow.xmin >> narrow.ymin >> narrow.xmax >> narrow.ymax),
             "Incomplete corridor coordinates");
    }
    originalFile >> std::ws;
    need(originalFile.eof(), "Unexpected trailing corridor data");
    const auto stem""")
    return once(src, """    std::ifstream narrowFile(stem + ".narrow");
    if (narrowFile.is_open()) {
        const Attempt narrow = readAttempt(narrowFile);
        need(nonBoxFields(narrow) == nonBoxFields(original)
             && (!original.cross || (original.north != original.south))""",
        """    if (hasCorridor) {
        need((!original.cross || (original.north != original.south))""")


def materialize(src):
    src = once(src, """                env0["pack"](step / f"member{channel}.attempt", attempt)
                if "corridor" in member:
                    env0["pack"](step / f"member{channel}.narrow", attempt | member["corridor"])""",
        """                class AttemptPacket:
                    def write_text(self, line):
                        b = member.get("corridor")
                        tail = "0" if b is None else "1 " + " ".join(
                            str(b[k]) for k in ("xMinBB", "yMinBB", "xMaxBB", "yMaxBB"))
                        (step / f"member{channel}.attempt").write_text(line + tail + "\\n")
                env0["pack"](AttemptPacket(), attempt)""")
    return once(src, '            bridge.send("answers", batch, {"members": answers})',
        '''            bridge.send("answers", batch, {"members": answers,
                        "device_width": concurrency["maximum_completed_width"]})''')

