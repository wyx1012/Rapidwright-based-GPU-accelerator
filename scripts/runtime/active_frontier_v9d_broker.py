
# Expose v9d only to the resident router and record its existing GPU receipt.
def replace_once(source, anchor, change):
    if source.count(anchor) != 1:
        raise RuntimeError("Unexpected v7 broker anchor: " + anchor.strip().splitlines()[0])
    return source.replace(anchor, change, 1)


def patch(source):
    source = replace_once(source,
        '    device, _ = launch("resident_cuda", args, env | {"CUDA_VISIBLE_DEVICES": uuid}, pipe=True)',
        '    device, _ = launch("resident_cuda", args, env | {"CUDA_VISIBLE_DEVICES": uuid, '
        '"FIDES_GPU_ACTIVE_FRONTIER_V9": "1"}, pipe=True)')
    source = replace_once(source, '    prior_seq = commits = multi = candidates = atomics = 0',
        '    prior_seq = commits = multi = candidates = atomics = 0\n'
        '    total_active_attempts = total_active_completed = total_active_reverted = 0')
    source = replace_once(source,
        '                                    gpu_trace_us=reply["PHASE_US"][1], gpu_projection_us=reply["BUILD"][1]))',
        '                                    gpu_trace_us=reply["PHASE_US"][1], gpu_projection_us=reply["BUILD"][1]))\n'
        '                if "ACTIVE_FRONTIER" in reply:\n'
        '                    records[-1]["active_frontier"] = reply["ACTIVE_FRONTIER"]')
    source = replace_once(source,
        '            stream.write(json.dumps(record, separators=(",", ":")) + "\\n")',
        '''            receipts = [entry["active_frontier"] for entry in records if "active_frontier" in entry]
            active_completed = sum(receipt[:2] == [1, 0] for receipt in receipts)
            active_reverted = sum(bool(receipt) and receipt[0] == 2 for receipt in receipts)
            first_active = active_completed > 0 and total_active_completed == 0
            record.update(active_frontier_attempts=len(receipts), active_frontier_completed=active_completed,
                          active_frontier_reverted=active_reverted)
            total_active_attempts += len(receipts)
            total_active_completed += active_completed
            total_active_reverted += active_reverted
            if first_active:
                publish(work / "active_frontier_first_commit.json", record)
            stream.write(json.dumps(record, separators=(",", ":")) + "\\n")''')
    source = replace_once(source, 'if batch <= 3 or first_overlap or now - last_update >= 20:',
                          'if batch <= 3 or first_overlap or first_active or now - last_update >= 20:')
    source = replace_once(source, '                     minimum_free_bytes=free)',
        '                     minimum_free_bytes=free, active_frontier_attempts=total_active_attempts,\n'
        '                     active_frontier_completed=total_active_completed,\n'
        '                     active_frontier_reverted=total_active_reverted)')
    return replace_once(source, '                atomic_requests=atomics, dcp=str(output), dcp_sha256=digest(output))',
        '                atomic_requests=atomics, dcp=str(output), dcp_sha256=digest(output),\n'
        '                active_frontier_attempts=total_active_attempts, active_frontier_completed=total_active_completed,\n'
        '                active_frontier_reverted=total_active_reverted)')
