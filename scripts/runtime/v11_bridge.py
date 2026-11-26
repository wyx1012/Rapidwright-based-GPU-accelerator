"""Keep Java↔broker batching in one ordered local socket; CUDA owns its binary data files."""
from contextlib import ExitStack
import json
import secrets
import select
import socket


class LocalBatchBridge:
    def __enter__(self):
        self.stack = ExitStack()
        with self.stack:
            # Corretto 11 supports loopback TCP. The per-run capability is passed only to Java.
            self.listener = self.stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            self.listener.bind(("127.0.0.1", 0))
            self.port = self.listener.getsockname()[1]
            self.token = secrets.token_urlsafe(32)
            self.listener.listen(1)
            self.connection = None
            self.buffer = bytearray()
            self.stack = self.stack.pop_all()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stack.__exit__(exc_type, exc, traceback)

    def _readable(self, fd, processes, release):
        while not select.select((fd,), (), (), 0.1)[0]:
            if release is not None and release.is_file():
                return False
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError(f"Process {process.pid} exited {process.returncode} during batch exchange")
        return True

    def attach(self, app, release=None):
        if not self._readable(self.listener, (app,), release):
            return False
        self.connection, _ = self.listener.accept()
        self.stack.enter_context(self.connection)
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        hello = self.receive("hello", 1, app)["payload"]
        if hello.get("pid") != app.pid or not secrets.compare_digest(hello.get("token", ""), self.token):
            raise RuntimeError("The local batch socket was not opened by the routing Java process")
        self.listener.close()
        return True

    def receive(self, expected, batch, *processes, release=None):
        newline = self.buffer.find(b"\n")
        while newline < 0:
            if not self._readable(self.connection, processes, release):
                if self.buffer:
                    raise RuntimeError("An incomplete batch frame preceded the CUDA phase handoff")
                return None
            data = self.connection.recv(65536)
            if not data:
                raise RuntimeError(f"Java closed the batch socket before {expected} {batch}")
            newline = data.find(b"\n")
            if newline >= 0:
                newline += len(self.buffer)
            self.buffer.extend(data)
        message = json.loads(self.buffer[:newline])
        del self.buffer[:newline + 1]
        if message.get("batch") != batch:
            raise RuntimeError(f"Unordered Java batch frame {message.get('batch')}, expected {batch}")
        kind = message.get("kind")
        if kind != expected and not (expected == "commit" and kind == "reject"):
            raise RuntimeError(f"Unexpected Java batch frame {kind!r}, expected {expected!r}")
        return message

    def send(self, kind, batch, payload):
        message = json.dumps(dict(kind=kind, batch=batch, payload=payload), separators=(",", ":"))
        self.connection.sendall(message.encode("utf-8") + b"\n")


def once(source, old, new):
    if source.count(old) != 1:
        raise RuntimeError("Unexpected v11 bridge anchor: " + old.strip().splitlines()[0])
    return source.replace(old, new, 1)


JAVA_BRIDGE = r'''
    private java.io.BufferedReader bridgeInput;
    private java.io.BufferedWriter bridgeOutput;

    private void sendBatchFrame(String kind, JSONObject payload) throws IOException {
        if (bridgeOutput == null) {
            int port = Integer.getInteger("fides.ppt.f4.batchPort");
            var connection = new java.net.Socket(java.net.InetAddress.getByName("127.0.0.1"), port);
            connection.setTcpNoDelay(true);
            var utf8 = java.nio.charset.StandardCharsets.UTF_8;
            bridgeInput = new java.io.BufferedReader(new java.io.InputStreamReader(connection.getInputStream(), utf8));
            bridgeOutput = new java.io.BufferedWriter(new java.io.OutputStreamWriter(connection.getOutputStream(), utf8));
            sendBatchFrame("hello", new JSONObject().put("pid", ProcessHandle.current().pid())
                    .put("token", System.getenv("FIDES_V11_BATCH_TOKEN")));
        }
        bridgeOutput.write(new JSONObject().put("kind", kind).put("batch", phase)
                .put("payload", payload).toString());
        bridgeOutput.newLine();
        bridgeOutput.flush();
    }

    private JSONArray readBatchAnswers() throws IOException {
        String line = bridgeInput.readLine();
        if (line == null) throw new java.io.EOFException("GPU broker closed the batch socket");
        JSONObject frame = new JSONObject(line);
        require(frame.getString("kind").equals("answers") && frame.getInt("batch") == phase,
                "Out-of-order GPU batch reply");
        return frame.getJSONObject("payload").getJSONArray("members");
    }

'''


def patch_capture(source):
    source = once(source, "    private JSONArray commits;\n", JAVA_BRIDGE + "    private JSONArray commits;\n")
    source = once(source, '            json("snapshot.json",new JSONObject()',
                  '            sendBatchFrame("snapshot", new JSONObject()')
    source = once(source, r'''            if (phase > 1) {
                // before() has closed the immutable delta and published snapshot.json.
                Files.writeString(step.resolve("sync_ready"), "ready\n", StandardOpenOption.CREATE_NEW);
            }
''', "")
    source = once(source, '            json("members.json", ', '            sendBatchFrame("request", ')
    source = once(source, r'''            Path answer = step.resolve("batch_answers.json");
            try (var watcher = answer.getFileSystem().newWatchService()) {
                step.register(watcher, java.nio.file.StandardWatchEventKinds.ENTRY_CREATE,
                        java.nio.file.StandardWatchEventKinds.ENTRY_MODIFY);
                Files.writeString(step.resolve("snapshot_ready"), "ready\n", StandardOpenOption.CREATE_NEW);
                while (!Files.isRegularFile(answer)) {
                    var key = watcher.take();
                    key.pollEvents();
                    if (!key.reset()) throw new IOException("GPU reply directory disappeared");
                }
            }
            JSONArray replies = new JSONObject(Files.readString(answer)).getJSONArray("members");''',
        '''            JSONArray replies = readBatchAnswers();''')
    source = once(source, '''        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Interrupted GPU batch", error);
''', "")
    source = once(source, '                json("host_reject.json",', '                sendBatchFrame("reject",')
    return once(source, '            json("host_batch_commit.json",', '            sendBatchFrame("commit",')


def patch_drive(source):
    source = once(source, '''def drive(java, cp, env, feature, shape, fields):
    from host_chain_v7 import BatchNotifications''',
        '''def drive(java, cp, env, feature, shape, fields):
    from v11_bridge import LocalBatchBridge

    with LocalBatchBridge() as bridge:
        return drive_with_bridge(java, cp, env, feature, shape, fields, bridge)


def drive_with_bridge(java, cp, env, feature, shape, fields, bridge):
    from host_chain_v7 import BatchNotifications''')
    source = once(source,
        '    app, native_log = launch("native_multichannel", args, env | {"CUDA_VISIBLE_DEVICES": uuid})',
        '''    args.insert(4, f"-Dfides.ppt.f4.batchPort={bridge.port}")
    app, native_log = launch("native_multichannel", args,
                             env | {"CUDA_VISIBLE_DEVICES": uuid, "FIDES_V11_BATCH_TOKEN": bridge.token})''')
    source = once(source, '''    first_path = root / "step1/snapshot_ready"
    check(wait(first_path, app, allow_exit=True), f"Java did not produce a first GPU batch; see {native_log}")
    first = read(first_path.parent / "snapshot.json")
''', '''    bridge.attach(app)
    first = bridge.receive("snapshot", 1, app)["payload"]
''')
    source = once(source, '''            first_marker = "snapshot_ready" if batch == 1 else "sync_ready"
            if not wait_for_batch(step / first_marker, app, device):
                break
            chain_started = time.perf_counter_ns()
            notifications.watch(step)
            pre = read(step / "snapshot.json")''',
        '''            if batch == 1:
                pre = first
            else:
                frame = bridge.receive("snapshot", batch, app, device,
                                       release=work / "indirect_device_release_requested")
                if frame is None:
                    check(not wait_for_batch(step / "snapshot_ready", app, device),
                          "The terminal CUDA handoff unexpectedly opened a batch")
                    break
                pre = frame["payload"]
            chain_started = time.perf_counter_ns()
            notifications.watch(step)''')
    source = once(source, '''                notifications.wait(step / "snapshot_ready", app, device)
            started_batch = chain_started if batch == 1 else time.perf_counter_ns()
            pack_started = time.perf_counter_ns()
            request = read(step / "members.json")''',
        '''            request = bridge.receive("request", batch, app, device)["payload"]
            started_batch = chain_started if batch == 1 else time.perf_counter_ns()
            pack_started = time.perf_counter_ns()''')
    source = once(source, '''            publish(step / "batch_answers.json", {"members": answers})
            commit = notifications.wait_any((step / "host_batch_commit.json", step / "host_reject.json"), app)
            kind, confirmed = commit.name, read(commit)
            if kind != "host_batch_commit.json":''',
        '''            bridge.send("answers", batch, {"members": answers})
            commit = bridge.receive("commit", batch, app, device)
            confirmed = commit["payload"]
            if commit["kind"] != "commit":''')
    # Preserve the existing GPU request/response timer, including its sync-ack boundary.
    # Measure the formerly unassigned boundary between ordered commits and the next snapshot.
    source = once(source, '    last_update = 0.0\n', '    last_update = 0.0\n    previous_chain_finished_ns = None\n')
    source = once(source, '                          host_chain_ns=time.perf_counter_ns()-chain_started,',
        '                          host_chain_ns=(chain_finished_ns := time.perf_counter_ns())-chain_started,')
    return once(source, '                          gpu_concurrency=concurrency)',
        '''                          gpu_concurrency=concurrency, batch_transport="loopback_socket")
            record["between_batches_ns"] = (
                chain_started - previous_chain_finished_ns if previous_chain_finished_ns is not None else 0
            )
            previous_chain_finished_ns = chain_finished_ns''')
