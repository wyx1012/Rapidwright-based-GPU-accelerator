"""Start the resident flood only after Java opens a batch with strict CPU proofs."""
from v11_window import replace_once as once


def patch_drive(source):
    from v15_transport import materialize
    from v15_patch import broker
    source = materialize(source)
    source = once(source, '''    from hybrid_v10_broker import CPU_BUDGET
    args.insert(4, f"-Dfides.ppt.f4.cpuProbeBudget={CPU_BUDGET}")
''', '''    args.insert(4, f"-Dfides.ppt.f4.inputBytes={dcp.stat().st_size}")
''')
    source = source.replace('cpu["cpu_local_commits"]', 'cpu["cpu_local_completed"]')
    source = source.replace('previous_cpu["cpu_local_commits"]', 'previous_cpu["cpu_local_completed"]')
    source = source.replace('final_counts["cpu_local_commits"]', 'final_counts["cpu_local_completed"]')
    source = once(source, '    cpu_fields = ("cpu_local_commits", "cpu_probe_attempts", "cpu_probe_pops", "cpu_probe_ns")',
        '''    cpu_fields = ("cpu_local_completed", "cpu_local_commits", "cpu_probe_attempts",
                  "cpu_probe_pops", "cpu_probe_ns")''')
    source = once(source, '''              and counts["cpu_local_commits"] <= counts["cpu_probe_attempts"]
              and counts["cpu_probe_pops"] <= CPU_BUDGET * counts["cpu_probe_attempts"],
              "Invalid cumulative CPU budget receipt")''', '''              and counts["cpu_local_commits"] <= counts["cpu_local_completed"] <= counts["cpu_probe_attempts"],
              "Invalid cumulative CPU routing receipt")''')
    source = once(source, 'and member["feature_score"] >= 0 and type(pops) is int and 0 <= pops <= CPU_BUDGET,',
        '''and member["feature_score"] >= 0 and type(pops) is int
                      and member["cpu_budget"] in (8000, 16384, 32768, 65536, 262144, 1048576) and pops > member["cpu_budget"],''')
    source = once(source, '''                                    cpu_pops_before_handoff=member["cpu_pops_before_handoff"],
                                    f1_original_retry''',
        '''                                    cpu_pops_before_handoff=member["cpu_pops_before_handoff"],
                                    cpu_budget=member["cpu_budget"], f1_original_retry''')
    start = source.index("    bridge.attach(app)")
    middle = source.index('    first = bridge.receive("snapshot", 1, app)["payload"]', start)
    end = source.index("\n    batch = 1\n", middle)
    initialization = source[middle:end].rstrip("\n")
    source = source[:start] + '''    release = work / "indirect_device_release_requested"
    first = device = free = None
    save("waiting_for_ppt11_gpu_admission", java_pid=app.pid, gpu_pid=None)
    if bridge.attach(app, release=release):
''' + "\n".join("    " + line for line in initialization.splitlines()) + '''
    else:
        check(read(release)["java_pid"] == app.pid, "Wrong Java phase handoff")
        publish(work / "indirect_device_released", {"java_pid": app.pid})
        save("indirect_flood_bypassed_or_drained", gpu_pid=None, active_pid=app.pid,
             active_log="native_multichannel.log")
''' + source[end:]
    source = once(source, "        while True:\n            step = root", "        while first is not None:\n            step = root")
    source = once(source, '''                    check(not wait_for_batch(step / "snapshot_ready", app, device),
                          "The terminal CUDA handoff unexpectedly opened a batch")''',
        '''                    check(not (step / "snapshot_ready").exists()
                          and read(release)["java_pid"] == app.pid, "Wrong Java flood release boundary")
                    device.stdin.write("finish\\n")
                    device.stdin.flush()
                    check(device.wait() == 0, "Resident indirect CUDA process did not drain")
                    publish(work / "indirect_device_released", {"java_pid": app.pid})
                    save("indirect_flood_drained", gpu_pid=None, active_pid=app.pid,
                         active_log="native_multichannel.log")''')
    source = once(source, "check(device.returncode == 0 and output.is_file()",
        "check((device is None or device.returncode == 0) and output.is_file()")
    source = once(source, '    check(all(final_counts[key] >= cpu[key] for key in cpu_fields)',
        '''    check(final_counts["cpu_probe_attempts"] == final_cpu["attempts"]
          and all(final_counts[key] >= cpu[key] for key in cpu_fields)''')
    return broker(once(source, '                total_commits=final_cpu["attempts"], **final_counts,',
        '''                total_attempts=final_cpu["attempts"],
                total_commits=commits + final_counts["cpu_local_commits"],
                indirect_resident_started=device is not None, **final_counts,'''))
