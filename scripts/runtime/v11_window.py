"""Compare the v11 production prefix with the existing v10 record on gpu02."""
import json

MILESTONES = (10000, 20000)


class NoBenefit(Exception):
    def __init__(self, summary):
        super().__init__("No measured whole-prefix benefit")
        self.summary = summary


def replace_once(source, anchor, updated):
    if source.count(anchor) != 1:
        raise RuntimeError("Unexpected v10 runner anchor: " + anchor.strip().splitlines()[0])
    return source.replace(anchor, updated, 1)


class Window:
    def __init__(self):
        self.batches = self.same_batches = self.same_members = self.same_active = 0
        self.gpu_ns = self.chain_ns = self.same_gpu_ns = self.same_edges = self.cross_members = 0

    def update(self, row):
        self.batches += 1
        self.gpu_ns += row["gpu_ns"]
        self.chain_ns += row["host_chain_ns"]
        members = row["members"]
        crossed = sum(member["cross_slr"] for member in members)
        self.cross_members += crossed
        if crossed == 0:
            self.same_batches += 1
            self.same_members += len(members)
            self.same_gpu_ns += row["gpu_ns"]
            self.same_edges += sum(member["candidate_edges"] for member in members)
            self.same_active += row["active_frontier_completed"]
        return dict(sequence=row["total_commits"], wall_ns=row["wall_from_java_ns"],
                    cpu_commits=row["cpu_local_commits"], cpu_probes=row["cpu_probe_attempts"],
                    cpu_pops=row["cpu_probe_pops"], cpu_probe_ns=row["cpu_probe_ns"],
                    **vars(self))


def archived_reference(path):
    tracker, points = Window(), {}
    with path.open("rb") as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                break
            current = tracker.update(json.loads(line))
            for target in MILESTONES:
                if current["sequence"] >= target and str(target) not in points:
                    points[str(target)] = current
            if len(points) == len(MILESTONES):
                return points
    raise RuntimeError("Incomplete existing v10 production prefix")


def patch_drive(source):
    source = replace_once(source, '"FIDES_GPU_ACTIVE_FRONTIER_V9": "1"}, pipe=True)',
        '"FIDES_GPU_ACTIVE_FRONTIER_V9": "1", "FIDES_GPU_SAME_SLR_FRONTIER_V11": "1", '
        '"FIDES_GPU_SAME_SLR_PARITY_V11": "1"}, pipe=True)')
    source = replace_once(source, '                    records[-1]["active_frontier"] = reply["ACTIVE_FRONTIER"]',
        '''                    records[-1]["active_frontier"] = reply["ACTIVE_FRONTIER"]
                if "SAME_SLR_PARITY" in reply:
                    records[-1]["same_slr_parity"] = reply["SAME_SLR_PARITY"]
                    publish(work / "same_slr_first_parity.json", dict(
                        sequence=attempt["sequence"], batch=batch, gpu_uuid=uuid,
                        parity=reply["SAME_SLR_PARITY"], active=reply.get("ACTIVE_FRONTIER"),
                        active_candidates=reply["ARM"][2]))''')
    source = replace_once(source, "            record.update(cpu, total_commits=",
        "            record[\"batch_benefit\"] = request[\"batch_benefit\"]\n"
        "            record.update(cpu, total_commits=")
    source = replace_once(source, "    last_update = 0.0",
        "    from v11_window import MILESTONES, NoBenefit, Window\n"
        "    reference_windows = read(work / \"reference_windows.json\")\n"
        "    v11_window, v11_milestones = Window(), iter(MILESTONES)\n"
        "    v11_next = next(v11_milestones)\n"
        "    last_update = 0.0")
    return replace_once(source, '            stream.write(json.dumps(record, separators=(",", ":")) + "\\n")',
        '''            stream.write(json.dumps(record, separators=(",", ":")) + "\\n")
            if v11_next is not None:
                prefix = v11_window.update(record)
                if prefix["sequence"] >= v11_next:
                    old = reference_windows[str(v11_next)]
                    per_commit = prefix["wall_ns"] / prefix["sequence"]
                    previous_per_commit = old["wall_ns"] / old["sequence"]
                    gpu_path_used = prefix["same_active"] > 0 and (work / "same_slr_first_parity.json").is_file()
                    observed_gain = 1 - per_commit / previous_per_commit
                    summary = dict(target=v11_next, v10=old, v11=prefix, positive=observed_gain > 0,
                                   wall_per_commit_gain=observed_gain, same_slr_active_used=gpu_path_used,
                                   gpu_uuid=uuid, iteration=pre["iteration"])
                    stream.flush()
                    publish(work / f"prefix_{v11_next}.json", summary)
                    v11_next = next(v11_milestones, None)
                    if v11_next is None and (observed_gain <= 0 or not gpu_path_used):
                        raise NoBenefit(summary)''')
