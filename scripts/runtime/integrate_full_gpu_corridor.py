"""Wire the accepted F1 planner into the eight-channel GPU-only route overlay."""

from pathlib import Path


def replace_once(source: str, before: str, after: str) -> str:
    if source.count(before) != 1:
        raise ValueError("Missing or ambiguous F1 source anchor: " + before.strip().splitlines()[0])
    return source.replace(before, after, 1)


def patch_router(source: str) -> str:
    source = replace_once(source, "    private F4FeatureDirect f4Direct;", """    private F4FeatureDirect f4Direct;
    private F1Corridor f4Corridor;

    F1Corridor.Box f4CorridorBox(Connection connection) {
        return f4Corridor.box(connection);
    }

    void f4RetireCorridor(Connection connection) {
        f4Corridor.disable(connection);
    }""")
    return replace_once(source,
        "        autoTuneRoutingProfileByDesignSize();",
        """        autoTuneRoutingProfileByDesignSize();
        if (System.getProperty("fides.ppt.f4.features") != null) {
            if (!config.isUseBoundingBox()) {
                throw new IllegalStateException("GPU-only F1 requires bounding boxes");
            }
            long started = System.nanoTime();
            f4Corridor = new F1Corridor(design);
            System.out.printf("F1_PPT_INITIAL_OCCUPANCY ns=%d%n", System.nanoTime() - started);
        }""")

def patch_planner(source: str) -> str:
    return replace_once(source,
        """        Box shortBox = new Box(Math.min(sx, tx) - 1, Math.min(sy, ty) - 1,
                Math.max(sx, tx) + 1, Math.max(sy, ty) + 1);
        return shortBox.same(base) ? null : shortBox;""",
        """        Box shortBox = new Box(Math.max(base.left, Math.min(sx, tx) - 1),
                Math.max(base.bottom, Math.min(sy, ty) - 1),
                Math.min(base.right, Math.max(sx, tx) + 1),
                Math.min(base.top, Math.max(sy, ty) + 1));
        return shortBox.same(base) ? null : shortBox;""")


def patch_capture(source: str) -> str:
    source = replace_once(source, """            JSONArray members = new JSONArray();
            for (int index = 0; index < attempts.size(); ++index) {""",
        """            JSONArray members = new JSONArray();
            F1Corridor.Box[] corridors = new F1Corridor.Box[attempts.size()];
            long corridorPlanNs = 0;
            for (int index = 0; index < attempts.size(); ++index) {""")
    source = replace_once(source,
        """                members.put(new JSONObject().put("attempt", rawAttempt(attempt)).put("feature_score", scores[index]));
            }
            json("members.json", new JSONObject().put("members", members));""",
        """                JSONObject member = new JSONObject().put("attempt", rawAttempt(attempt))
                        .put("feature_score", scores[index]);
                if (attempt.useBoundingBox && !attempt.crossSLR) {
                    long started = System.nanoTime();
                    F1Corridor.Box box = router.f4CorridorBox(states.get(index).connection);
                    corridorPlanNs += System.nanoTime() - started;
                    if (box != null) {
                        require(box.left >= attempt.xMinBB && box.right <= attempt.xMaxBB
                                && box.bottom >= attempt.yMinBB && box.top <= attempt.yMaxBB
                                && box.right - box.left >= 2 && box.top - box.bottom >= 2
                                && (box.left > attempt.xMinBB || box.right < attempt.xMaxBB
                                    || box.bottom > attempt.yMinBB || box.top < attempt.yMaxBB),
                                "GPU corridor differs from the original attempt bounds");
                        corridors[index] = box;
                        member.put("corridor", new JSONObject().put("xMinBB", box.left).put("yMinBB", box.bottom)
                                .put("xMaxBB", box.right).put("yMaxBB", box.top));
                    }
                }
                members.put(member);
            }
            json("members.json", new JSONObject().put("members", members).put("f1_plan_ns", corridorPlanNs));""")
    return replace_once(source,
        """                paths.add(validate(replies.getJSONObject(index), graph, rrg, ids, state.connection, router, state));""",
        """                JSONObject reply = replies.getJSONObject(index);
                F1Corridor.Box box = corridors[index];
                boolean retry = reply.getBoolean("f1_original_retry");
                require(!retry || box != null, "Unexpected original-box GPU retry");
                BatchPath path = validate(reply, graph, rrg, ids, state.connection, router, state);
                if (retry) {
                    router.f4RetireCorridor(state.connection);
                } else if (box != null) {
                    for (int hop = 1; hop + 1 < path.ids().length; ++hop) {
                        int nodeId = path.ids()[hop];
                        require(box.contains(rrg.endTileX[nodeId], rrg.endTileYRaw[nodeId]),
                                "GPU path escaped the selected corridor");
                    }
                }
                paths.add(path);""")


def patch_broker(source: str) -> str:
    source = replace_once(source,
        """            members = read(step / "members.json")["members"]""",
        """            request = read(step / "members.json")
            members = request["members"]""")
    source = replace_once(source,
        """                env0["pack"](step / f"member{channel}.attempt", attempt)""",
        """                env0["pack"](step / f"member{channel}.attempt", attempt)
                if "corridor" in member:
                    env0["pack"](step / f"member{channel}.narrow", attempt | member["corridor"])""")
    source = replace_once(source,
        """                cost, *path = reply["PATH"]""",
        """                retry = reply["F1_RETRY"]
                check(retry == [0] or (retry == [1] and "corridor" in member and "F1_DISCARDED" in reply),
                      "Unexpected original-box GPU retry")
                cost, *path = reply["PATH"]""")
    source = replace_once(source,
        """                                    ingress=reply["INGRESS"]))""",
        """                                    ingress=reply["INGRESS"], f1_original_retry=retry == [1]))""")
    source = replace_once(source,
        """                                    channel=channel, candidate_edges=reply["ARM"][2],""",
        """                                    channel=channel, f1_narrowed="corridor" in member,
                                    f1_original_retry=retry == [1], candidate_edges=reply["ARM"][2],""")
    return replace_once(source,
        """                          gpu_overlap_mask=overlap, batch_ns=time.perf_counter_ns()-started_batch,""",
        """                          gpu_overlap_mask=overlap, batch_ns=time.perf_counter_ns()-started_batch,
                          f1_plan_ns=request["f1_plan_ns"],""")


def prepare(reference: Path, work: Path) -> list[str]:
    planner = reference.parent / "ppt-pipeline-v1-r3" / "F1Corridor.java"
    sources = ((reference / "RWRoute.java", patch_router),
               (reference / "F4Theia50perCapture.java", patch_capture),
               (planner, patch_planner))
    for original, patch in sources:
        (work / original.name).write_text(patch(original.read_text(encoding="utf-8")), encoding="utf-8")
    return [original.name for original, _ in sources]
