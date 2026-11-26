"""Overlap the ordered mirror upload with preparation of the same GPU batch."""
import ctypes
import os
from pathlib import Path
import select
import time


class BatchNotifications:
    """Wait on published files and periodically check the owning subprocess."""

    def __enter__(self):
        self.fd = -1
        self.wd = -1
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            initialize = libc.inotify_init1
            self.add_watch = libc.inotify_add_watch
            self.remove_watch = libc.inotify_rm_watch
        except AttributeError:
            return self
        initialize.argtypes = (ctypes.c_int,)
        self.add_watch.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
        self.remove_watch.argtypes = (ctypes.c_int, ctypes.c_int)
        self.fd = initialize(os.O_NONBLOCK | os.O_CLOEXEC)
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.fd >= 0:
            os.close(self.fd)

    def watch(self, directory):
        if self.fd < 0:
            return
        if self.wd >= 0:
            self.remove_watch(self.fd, self.wd)
        # Java and CUDA publish files by create, close or same-directory rename.
        self.wd = self.add_watch(self.fd, os.fsencode(directory), 0x0008 | 0x0080 | 0x0100)

    def _pause(self):
        if self.wd < 0:
            time.sleep(0.005)
        elif select.select([self.fd], [], [], 0.05)[0]:
            try:
                os.read(self.fd, 65536)
            except BlockingIOError:
                pass

    def wait_any(self, paths, *processes):
        while True:
            for path in paths:
                if path.is_file():
                    return path
            for process in processes:
                if process.poll() is not None:
                    # A successful publish can immediately precede process exit.
                    for path in paths:
                        if path.is_file():
                            return path
                    names = ", ".join(path.name for path in paths)
                    raise RuntimeError(f"Process {process.pid} exited {process.returncode} before {names}")
            self._pause()

    def wait(self, path, *processes):
        return self.wait_any((path,), *processes)


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("Unexpected host-chain anchor: " + old.strip().splitlines()[0])
    return source.replace(old, new, 1)


def patch_capture(source):
    anchor = """        int sequence = -1;
        try {
            JSONArray members = new JSONArray();"""
    return replace_once(source, anchor, """        int sequence = -1;
        try {
            if (phase > 1) {
                // before() has closed the immutable delta and published snapshot.json.
                Files.writeString(step.resolve("sync_ready"), "ready\\n", StandardOpenOption.CREATE_NEW);
            }
            JSONArray members = new JSONArray();""")


def patch_drive(source):
    source = replace_once(source,
        'def drive(java, cp, env, feature, shape, fields):',
        'def drive(java, cp, env, feature, shape, fields):\n'
        '    from host_chain_v7 import BatchNotifications')
    source = replace_once(source,
        '    with (work/"dispatch.jsonl").open("x", buffering=1 << 18) as stream:',
        '    with BatchNotifications() as notifications, (work/"dispatch.jsonl").open("x", buffering=1 << 18) as stream:')
    source = replace_once(source, """            step = root / f"step{batch}"
            if not wait_for_batch(step / "snapshot_ready", app, device):
                break
            started_batch = time.perf_counter_ns()
            pre = read(step / "snapshot.json")
            request = read(step / "members.json")""",
        """            step = root / f"step{batch}"
            first_marker = "snapshot_ready" if batch == 1 else "sync_ready"
            if not wait_for_batch(step / first_marker, app, device):
                break
            chain_started = time.perf_counter_ns()
            notifications.watch(step)
            pre = read(step / "snapshot.json")
            sync_started = None
            if batch > 1:
                delta, history, refresh = pre["delta_rows"], pre["history_rows"], int(pre["refresh_pcc"])
                check((step / "between.bin").stat().st_size == 28*delta, "State delta size")
                sync_started = time.perf_counter_ns()
                device.stdin.write(f"sync {delta} {history} {refresh}\\n")
                device.stdin.flush()
                notifications.wait(step / "snapshot_ready", app, device)
            started_batch = chain_started if batch == 1 else time.perf_counter_ns()
            pack_started = time.perf_counter_ns()
            request = read(step / "members.json")""")
    source = replace_once(source, """            sync_started = time.perf_counter_ns()
            if batch > 1:
                delta, history, refresh = pre["delta_rows"], pre["history_rows"], int(pre["refresh_pcc"])
                check((step / "between.bin").stat().st_size == 28*delta, "State delta size")
                device.stdin.write(f"sync {delta} {history} {refresh}\\n")
                device.stdin.flush()
                wait(step / "device_synced", device)
                check(int((step / "device_synced").read_text()) == history, "History acknowledgement")
            gpu_started = time.perf_counter_ns()""",
        """            pack_finished = time.perf_counter_ns()
            if sync_started is not None:
                notifications.wait(step / "device_synced", device)
                check(int((step / "device_synced").read_text()) == history, "History acknowledgement")
            gpu_started = time.perf_counter_ns()""")
    source = replace_once(source,
        '            wait(step / "device_batch.txt", device)',
        '            notifications.wait(step / "device_batch.txt", device)')
    source = replace_once(source,
        '            kind, confirmed = await_commit(step, app)',
        '''            commit = notifications.wait_any((step / "host_batch_commit.json", step / "host_reject.json"), app)
            kind, confirmed = commit.name, read(commit)''')
    return replace_once(source,
        '                          sample_free_bytes=sample_free, sync_ns=gpu_started-sync_started,',
        """                          sample_free_bytes=sample_free,
                          sync_ns=(gpu_started-sync_started) if sync_started is not None else 0,
                          host_sync_blocked_ns=(gpu_started-pack_finished) if sync_started is not None else 0,
                          host_pack_ns=pack_finished-pack_started,
                          host_chain_ns=time.perf_counter_ns()-chain_started,""")
