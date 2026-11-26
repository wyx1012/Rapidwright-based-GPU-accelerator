"""Measure host critical-chain changes against the already stopped v11 prefix."""
from v11_window import patch_drive as patch_v11, replace_once as once

MILESTONES = (20000, 50000, 100000)
HOST_COUNTERS = ("host_pairs", "host_reused", "host_pair_ns", "host_conflicts", "host_cold_or_miss")


def patch_drive(source):
    source = patch_v11(source)
    source = once(source, "from v11_window import MILESTONES, NoBenefit, Window",
                  "from v11_window import NoBenefit, Window\n    from v12_window import MILESTONES, HOST_COUNTERS")
    source = once(source, '"FIDES_GPU_SAME_SLR_PARITY_V11": "1"}, pipe=True)',
                  '"FIDES_GPU_SAME_SLR_PARITY_V11": "0"}, pipe=True)')
    source = once(source, '            record["batch_benefit"] = request["batch_benefit"]',
                  """            record["batch_benefit"] = request["batch_benefit"]
            record["host_schedule"] = {key: request[key] for key in HOST_COUNTERS}
            record["host_journal"] = pre["journal"]""")
    source = once(source,
        '    reference_windows = read(work / "reference_windows.json")',
        '    reference_windows = read(work / "reference_windows.json")\n    v12_gap_ns = 0')
    source = once(source, "                prefix = v11_window.update(record)",
                  """                v12_gap_ns += record["between_batches_ns"]
                prefix = v11_window.update(record)
                prefix.update(host_gap_ns=v12_gap_ns, host_journal=record["host_journal"],
                              host_schedule=record["host_schedule"])""")
    source = once(source, '                    gpu_path_used = prefix["same_active"] > 0 and (work / "same_slr_first_parity.json").is_file()',
                  '                    gpu_path_used = prefix["same_active"] > 0')
    source = once(source, "summary = dict(target=v11_next, v10=old, v11=prefix,",
                  "summary = dict(target=v11_next, v11=old, v12=prefix,")
    return once(source, "                    if v11_next is None and (observed_gain <= 0 or not gpu_path_used):",
        """                    if (summary["target"] == 50000 and observed_gain <= 0
                            or v11_next is None and (observed_gain <= 0 or not gpu_path_used)):""")
