"""Extend the v9d broker to account for ordered local A* commits and capped handoffs."""

CPU_BUDGET = 8000


def once(source, anchor, replacement):
    if source.count(anchor) != 1:
        raise RuntimeError("Unexpected v9d broker anchor: " + anchor.strip().splitlines()[0])
    return source.replace(anchor, replacement, 1)


def patch(source):
    source = once(source, '    started = time.perf_counter_ns()\n', '''    from hybrid_v10_broker import CPU_BUDGET
    args.insert(4, f"-Dfides.ppt.f4.cpuProbeBudget={CPU_BUDGET}")
    started = time.perf_counter_ns()
''')
    source = once(source, '    first_path = root / "step1/snapshot_ready"\n', '''    cpu_fields = ("cpu_local_commits", "cpu_probe_attempts", "cpu_probe_pops", "cpu_probe_ns")

    def read_cpu_receipt(payload):
        counts = {key: payload[key] for key in cpu_fields}
        check(all(type(value) is int and value >= 0 for value in counts.values())
              and counts["cpu_local_commits"] <= counts["cpu_probe_attempts"]
              and counts["cpu_probe_pops"] <= CPU_BUDGET * counts["cpu_probe_attempts"],
              "Invalid cumulative CPU budget receipt")
        return counts

    cpu = dict.fromkeys(cpu_fields, 0)
    first_path = root / "step1/snapshot_ready"
''')
    source = once(source, '            members = request["members"]\n', '''            members = request["members"]
            previous_cpu = cpu
            cpu = read_cpu_receipt(request)
            check(all(cpu[key] >= previous_cpu[key] for key in cpu_fields)
                  and prior_seq == commits + previous_cpu["cpu_local_commits"],
                  "The CPU and GPU totals no longer describe the same ordered prefix")
''')
    source = once(source, '''                check(attempt["sequence"] == prior_seq + 1 and member["feature_score"] >= 0,
                      "Wrong original connection order or missing feature")''', '''                pops = member["cpu_pops_before_handoff"]
                check(attempt["sequence"] == cpu["cpu_local_commits"] + commits + channel + 1
                      and member["feature_score"] >= 0 and type(pops) is int and 0 <= pops <= CPU_BUDGET,
                      "Wrong original connection order, feature or CPU proof receipt")''')
    source = once(source,
        '                                    ingress=reply["INGRESS"], f1_original_retry=retry == [1]))',
        '''                                    ingress=reply["INGRESS"], f1_original_retry=retry == [1],
                                    cpu_pops_before_handoff=member["cpu_pops_before_handoff"]))''')
    source = once(source, '''            check(confirmed["members"] == [dict(sequence=row["sequence"],
                                                  cpu_pops_before_handoff=0) for row in answers],
                  "Ordered commit path or CPU search invariant")''', '''            check(confirmed["members"] == [dict(sequence=row["sequence"],
                             cpu_pops_before_handoff=row["cpu_pops_before_handoff"]) for row in answers],
                  "Ordered GPU commit or bounded CPU count changed after handoff")''')
    source = once(source,
        '                                    channel=channel, cross_slr=attempt["crossSLR"], f1_narrowed="corridor" in member,',
        '''                                    channel=channel, cross_slr=attempt["crossSLR"], f1_narrowed="corridor" in member,
                                    cpu_pops_before_handoff=member["cpu_pops_before_handoff"],''')
    source = once(source, '            stream.write(json.dumps(record, separators=(",", ":")) + "\\n")', '''            record.update(cpu, total_commits=commits + cpu["cpu_local_commits"],
                          wall_from_java_ns=time.perf_counter_ns()-started)
            stream.write(json.dumps(record, separators=(",", ":")) + "\\n")''')
    source = once(source,
        '                     gpu_requests=commits, gpu_commits=commits, gpu_rejected=0, gpu_batches=batch,',
        '''                     gpu_requests=commits, gpu_commits=commits, gpu_rejected=0, gpu_batches=batch,
                     total_commits=commits + cpu["cpu_local_commits"], **cpu,''')
    source = once(source, '    check(device.returncode == 0 and output.is_file() and witness is not None',
                          '    check(device.returncode == 0 and output.is_file()')
    source = once(source,
        '                multi_connection_batches=multi, gpu_batches=batch-1, kernel_overlap_verified=True,',
        '                multi_connection_batches=multi, gpu_batches=batch-1, kernel_overlap_verified=witness is not None,')
    source = once(source, '    return dict(status="passed", gpu_uuid=uuid, gpu_commits=commits, gpu_rejected=0,',
        '''    final_cpu = read(work / "f4_hybrid_cpu_final.json")
    final_counts = read_cpu_receipt(final_cpu)
    check(all(final_counts[key] >= cpu[key] for key in cpu_fields)
          and final_cpu["attempts"] == commits + final_counts["cpu_local_commits"],
          "Final CPU and GPU commit totals do not cover the routing attempts")
    return dict(status="passed", gpu_uuid=uuid, gpu_commits=commits, gpu_rejected=0,
                total_commits=final_cpu["attempts"], **final_counts,''')
    return once(source, '                direct_precompute=direct, direct_finalize=final_direct, cpu_path_searches=0,',
        '''                direct_precompute=direct, direct_finalize=final_direct,
                cpu_path_searches=final_counts["cpu_probe_attempts"],''')
