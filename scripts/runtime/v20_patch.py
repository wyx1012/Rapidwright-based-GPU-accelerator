"""Adapt the pinned v19 startup to a preloaded GPU and live-first handoff."""
from v11_window import replace_once as once


def cuda(src, directory):
    src = once(src, "void* reserveDevice(size_t bytes) {",
               (directory / "V20Resident.inc").read_text() + "void* reserveDevice(size_t bytes) {")
    src = once(src, "    need(allocatedBytes <= allocationBudget,",
               '''    v20Event("before-alloc", bytes);
    need(allocatedBytes <= allocationBudget,''')
    src = once(src, '''        const auto last = cudaGetLastError();
        if (last != cudaErrorMemoryAllocation) checkCuda(last);''',
               '''        const auto last = cudaGetLastError();
        v20Event("cudaMallocAsync", bytes, status);
        if (last != cudaErrorMemoryAllocation) checkCuda(last);''')
    src = once(src, '''    checkCuda(status);
    allocatedBytes += bytes;''', '''    v20Event("cudaMallocAsync", bytes, status);
    checkCuda(status);
    allocatedBytes += bytes;''')
    src = once(src, '''void publish(const std::string& path, const std::string& content) {''',
               (directory / "V20Snapshot.inc").read_text() + '''
void publish(const std::string& path, const std::string& content) {''')
    # Define the bounded input transfer before the existing access-column loader,
    # which can share it without changing the normal route-time I/O.
    begin = src.index("template<class T> void v20Upload")
    end = src.index("void publish(const std::string& path, const std::string& content)", begin)
    helpers = src[begin:end]
    src = src[:begin] + src[end:]
    src = once(src, "template<class T> T* loadRrg(",
               helpers + "template<class T> T* loadRrg(")
    src = once(src, '''template<class T> T* loadRrg(std::ifstream& file, U64 at, size_t n) {
    file.seekg(at);
    return upload(readWords<T>(file,n));
}''', '''template<class T> T* loadRrg(std::ifstream& file, U64 at, size_t n, const char* name) {
    file.seekg(at);
    if (v20Startup) return v20Load<T>(name, &file, n);
    return upload(readWords<T>(file,n));
}''')
    src = once(src, '''                  const std::string& slrPath, U32 nodes, U64& bytes) {''',
               '''                  const std::string& slrPath, U32 nodes, U64& bytes,
                  bool prepared, U32& savedSlrWords, U32& savedTypes) {''')
    src = once(src, '''    auto slr = readWords<int>(slrFile,slrWords);
    need((slrWords==6 && slr[0]==0)
          || (slr[0]==1 && slr[2]>0 && slr[3]>0 && slr[4]==int(types) && slr[5]>0
              && U32(6+slr[2]+2*slr[3]+slr[4]+slr[5])==slrWords),"Packed SLR table");''',
               '''    savedSlrWords = slrWords;
    savedTypes = types;
    std::vector<int> slr;
    if (!prepared) slr = v20ReadSlr(slrFile, slrWords, types);''')
    src = once(src, '''    return Access{
        loadRrg<int>(file,at[0],nodes), loadRrg<int>(file,at[1],nodes),
        loadRrg<int>(file,at[2],nodes), loadRrg<int>(file,at[3],nodes),
        loadRrg<int>(file,at[4],nodes), loadRrg<int>(file,at[5],nodes),
        loadRrg<int>(file,at[6],nodes), loadRrg<int>(file,at[7],nodes),
        upload(slr), loadRrg<U32>(file,at[8],nodes), loadRrg<U32>(file,at[9],nodes),
        upload(typeFlags),upload(tileFlags),upload(ord)};''', '''    return Access{
        loadRrg<int>(file,at[0],nodes,"nodeType"), loadRrg<int>(file,at[1],nodes,"intentCode"),
        loadRrg<int>(file,at[2],nodes,"tileType"), loadRrg<int>(file,at[3],nodes,"tileId"),
        loadRrg<int>(file,at[4],nodes,"endTileX"), loadRrg<int>(file,at[5],nodes,"endTileYRaw"),
        loadRrg<int>(file,at[6],nodes,"tileY"), loadRrg<int>(file,at[7],nodes,"length"),
        prepared ? v20Load<int>("slr", nullptr, slrWords) : v20UploadVector("slr", slr),
        loadRrg<U32>(file,at[8],nodes,"baseCost"), loadRrg<U32>(file,at[9],nodes,"delay"),
        v20UploadVector("typeFlags", typeFlags), v20UploadVector("tileFlags", tileFlags),
        v20UploadVector("ordinals", ord)};''')
    src = once(src, '''    const std::string out(argv[5]);
    const std::string dir(argv[1]);''', '''    std::string out(argv[5]);
    std::string dir(argv[1]);
    const char* preparation = std::getenv("FIDES_V20_RRG_OFFSETS");
    v20Startup = preparation != nullptr;''')
    src = once(src, "    checkCuda(cudaGetDeviceCount(&count));",
               '    v20Check("cudaGetDeviceCount", 0, cudaGetDeviceCount(&count));')
    src = once(src, "    checkCuda(cudaGetDeviceProperties(&properties, 0));",
               '    v20Check("cudaGetDeviceProperties", 0, cudaGetDeviceProperties(&properties, 0));')
    src = once(src, '''    checkCuda(cudaMemGetInfo(&freeBytes, &totalBytes));
    int poolsSupported = 0;
    checkCuda(cudaDeviceGetAttribute(&poolsSupported, cudaDevAttrMemoryPoolsSupported, 0));''',
               '''    v20Check("cudaMemGetInfo", 0, cudaMemGetInfo(&freeBytes, &totalBytes));
    v20Event("context-ready", 0);
    int poolsSupported = 0;
    v20Check("cudaDeviceGetAttribute", 0, cudaDeviceGetAttribute(&poolsSupported, cudaDevAttrMemoryPoolsSupported, 0));''')
    src = once(src, '''    Mutable data{
        loadColumn<U32>(dir,"offset.i32le",size_t(nodes)+1),
        loadColumn<U32>(dir,"edge.i32le",edges),
        loadColumn<int>(dir,"occ.i32le",nodes),
        loadColumn<int>(dir,"same.i32le",nodes),
        loadColumn<U32>(dir,"hist.i32le",nodes),
        loadColumn<U32>(dir,"pcc.i32le",pccSize),
        allocate<unsigned char>(nodes), nodes, pccSize};
    checkCuda(setInStream(data.dirty, 0, nodes));''', '''    U64 offsetAt=0, edgeAt=0, occAt=0, histAt=0, pccAt=0;
    std::ifstream rrgInput;
    if (preparation) {
        std::istringstream layout(preparation);
        need(bool(layout >> offsetAt >> edgeAt >> occAt >> histAt >> pccAt), "V20 RRG column offsets");
        rrgInput.open(argv[6], std::ios::binary);
        need(bool(rrgInput), "V20 cannot open static RRG");
        const size_t minimum = csrBytes + stateBytes + v20AccessBytes(argv[7], nodes) + csrBytes;
        v20Event("minimum-resident-budget", minimum);
        needMemory(minimum <= freeBytes - GpuReserve, "V20 shared data and one channel breach one-GiB reserve");
    }
    Mutable data{
        preparation ? loadRrg<U32>(rrgInput,offsetAt,size_t(nodes)+1,"offset") : loadColumn<U32>(dir,"offset.i32le",size_t(nodes)+1),
        preparation ? loadRrg<U32>(rrgInput,edgeAt,edges,"edge") : loadColumn<U32>(dir,"edge.i32le",edges),
        preparation ? loadRrg<int>(rrgInput,occAt,nodes,"occ") : loadColumn<int>(dir,"occ.i32le",nodes),
        preparation ? v20Load<int>("same",nullptr,nodes) : loadColumn<int>(dir,"same.i32le",nodes),
        preparation ? loadRrg<U32>(rrgInput,histAt,nodes,"hist") : loadColumn<U32>(dir,"hist.i32le",nodes),
        preparation ? loadRrg<U32>(rrgInput,pccAt,pccSize,"pcc") : loadColumn<U32>(dir,"pcc.i32le",pccSize),
        v20Load<unsigned char>("dirty",nullptr,nodes), nodes, pccSize};''')
    src = once(src, '''    auto access = loadAccess(argv[6],argv[7],argv[8],nodes,accessBytes);''',
               '''    U32 slrWords=0, slrTypes=0;
    auto access = loadAccess(argv[6],argv[7],argv[8],nodes,accessBytes,preparation != nullptr,slrWords,slrTypes);''')
    src = once(src, '''    auto activity = allocate<U32>(MaxChannels + 1);''',
               '''    U32* activity;
    {
        V20Column column("activity");
        activity = allocate<U32>(MaxChannels + 1);
    }''')
    src = once(src, '''            try {
                map = allocate<U32>(mapEntries);''', '''            V20Column column("channel_state");
            try {
                map = allocate<U32>(mapEntries);''')
    src = once(src, "            checkCuda(setInStream(map, 0xff, mapBytes));",
               '            v20Check("cudaMemsetAsync", mapBytes, setInStream(map, 0xff, mapBytes));')
    src = once(src, '''        checkCuda(cudaStreamSynchronize(cudaStreamPerThread));
    };
    auto trimMaps''', '''        V20Column column("channel_state");
        v20Check("cudaStreamSynchronize", mapBytes * discovered.size(), cudaStreamSynchronize(cudaStreamPerThread));
    };
    auto trimMaps''')
    src = once(src, "    growMaps(channels);", "    growMaps(preparation ? 1 : channels);")
    src = once(src, '''        const bool scored = operation == "route_scored";
        need((operation == "route" || scored)''', '''        const bool growthGranted = operation == "route_scored_grow";
        const bool scored = operation == "route_scored" || growthGranted;
        need((operation == "route" || scored)''')
    src = once(src, '''        if (cleanBatches == BatchesBeforeGrowing && members > discovered.size()) {''',
               '''        if ((!preparation || growthGranted) && cleanBatches == BatchesBeforeGrowing
                && members > discovered.size()) {''')
    src = once(src, '''    publish(out + "/resident_ready", "ready\\n");

    for (U32 phase''', '''    v20ReserveFloor("preflight-ready");
    publish(out + "/resident_ready", "ready\\n");
    if (preparation) {
        std::string command;
        need(bool(std::cin >> command), "V20 lost startup command");
        if (command == "finish") return 0;
        need(command == "load_first" && bool(std::cin >> dir), "V20 expected first live snapshot");
        out = dir;
        v20LiveColumn(dir,"occ.i32le",data.occ,nodes);
        v20LiveColumn(dir,"same.i32le",data.same,nodes);
        v20LiveColumn(dir,"hist.i32le",data.hist,nodes);
        v20LiveColumn(dir,"pcc.i32le",data.pcc,pccSize);
        std::ifstream slrFile(dir + "/slr.i32le", std::ios::binary);
        auto slr = v20ReadSlr(slrFile,slrWords,slrTypes);
        {
            V20Column column("slr");
            v20Check("cudaMemcpyAsync", slr.size()*sizeof(int),
                     cudaMemcpyAsync(const_cast<int*>(access.slr),slr.data(),slr.size()*sizeof(int),
                                     cudaMemcpyHostToDevice,cudaStreamPerThread));
            v20Check("cudaStreamSynchronize", slr.size()*sizeof(int),cudaStreamSynchronize(cudaStreamPerThread));
        }
        v20ReserveFloor("live-snapshot-ready");
        publish(out + "/resident_snapshot_loaded", "ready\\n");
    }
    v20Startup = false;

    for (U32 phase''')
    return src


def router(src):
    src = once(src, '''        if (hpwl < 16) return null;
''', '')
    src = once(src, '''        if (cost == null || cost.cpuSamples < 2) return 0;
''', '''        if (cost == null) return 0;
        // Exactly one exploratory, strictly proven connection may start pricing.
        if (!f4GpuCriticalResident) return F4_GPU_TRIAL;
        if (cost.cpuSamples < 2) return 0;
''')
    src = once(src, '''            f4GpuColdActual = elapsed;
''', '''            f4GpuColdActual = elapsed;
            f4GpuLearningBalance += Long.getLong("fides.ppt.f4.prewarmNs", 0L);
''')
    src = once(src, '''    private boolean f4GpuCriticalResident;''', '''    private boolean f4GpuCriticalResident;
    private boolean f4LatestWarmGain;

    boolean f4ResidentGrowthEarned() {
        return f4GpuGained > 0 && f4LatestWarmGain && f4GpuLearningBalance == 0;
    }''')
    src = once(src, '''        f4GpuLearningBalance = Math.max(0, f4GpuLearningBalance + elapsed - cpuTail);''',
               '''        f4GpuLearningBalance = Math.max(0, f4GpuLearningBalance + elapsed - cpuTail);
        f4LatestWarmGain = !cold && cpuTail > elapsed;''')
    return src


def capture(src):
    src = once(src, '''                dump("offset.i32le",rrg.offset,null);
                dump("edge.i32le",rrg.edge,null);''', '''                if (!Boolean.getBoolean("fides.ppt.f4.residentStaticRrg")) {
                    dump("offset.i32le",rrg.offset,null);
                    dump("edge.i32le",rrg.edge,null);
                }''')
    src = once(src, "    private int lastIteration;",
               "    private int lastIteration;\n    private boolean v20GrowthEarned;")
    src = once(src, '''        before(attempts.get(0), iteration, rrg, mirror, pcc, slr,''',
               '''        v20GrowthEarned = attempts.size() > 1 && router.f4ResidentGrowthEarned();
        before(attempts.get(0), iteration, rrg, mirror, pcc, slr,''')
    return once(src, '''                    .put("journal", journal.receipt()));''',
                '''                    .put("journal", journal.receipt()).put("v20_growth_earned", v20GrowthEarned));''')


def drive(src):
    start = src.index('    ords = fields(run("access_ordinals",')
    stop = src.index("    args =", start)
    src = src[:start] + src[stop:]
    src = once(src, '    app, native_log = launch("native_multichannel", args,',
               '''    args.insert(4, f"-Dfides.ppt.f4.prewarmNs={prepared_gpu['startup_ns']}")
    args.insert(4, "-Dfides.ppt.f4.residentStaticRrg=true")
    check(prepared_gpu["process"].poll() is None, "Prepared CUDA exited before Java")
    app, native_log = launch("native_multichannel", args,''')
    src = once(src, '''    save("waiting_for_ppt11_gpu_admission", java_pid=app.pid, gpu_pid=None)''',
               '''    save("waiting_for_ppt11_gpu_admission", java_pid=app.pid,
         gpu_pid=prepared_gpu["process"].pid)''')
    start = src.index('        slr = root / "slr.i32le"')
    stop = src.index('\n    else:\n        check(read(release)', start)
    src = src[:start] + '''        check(first["initial_pcc_words"] == prepared_gpu["pcc"], "Wrong PCC length at first handoff")
        device = prepared_gpu["process"]
        v20handoff.load_first(device, root, prepared_gpu, app)
        free = int(gpu("gpu", "memory.free")) << 20
        check(free >= 1 << 30, "One-GiB reserve after the first live snapshot")
        save("resident_live_snapshot_loaded", java_pid=app.pid, gpu_pid=device.pid,
             gpu_uuid=uuid, first_snapshot_free_bytes=free)
''' + src[stop:]
    src = once(src, '''    else:
        check(read(release)["java_pid"] == app.pid, "Wrong Java phase handoff")''',
                '''    else:
        v20handoff.finish(prepared_gpu["process"])
        check(read(release)["java_pid"] == app.pid, "Wrong Java phase handoff")''')
    return once(src, '''                device.stdin.write(f"route_scored {batch} {len(members)} {score_tokens}\\n")''',
                '''                operation = ("route_scored_grow" if len(members) > 1
                             and pre.get("v20_growth_earned") is True else "route_scored")
                device.stdin.write(f"{operation} {batch} {len(members)} {score_tokens}\\n")''')


def worker(src):
    src = once(src, "from score_split_v6 import patch_broker as score_broker",
               '''import v20_handoff as v20handoff
from v20_patch import drive as v20_drive
from score_split_v6 import patch_broker as score_broker''')
    src = once(src, '''    return window_broker(bridge_broker(hybrid_broker(active_broker(patch_host_drive(score_broker(source))))))''',
               '''    return v20_drive(window_broker(bridge_broker(hybrid_broker(active_broker(patch_host_drive(score_broker(source)))))))''')
    src = once(src, '''def lease_available_device():
    global uuid''', '''def lease_available_device(rejected, minimum_mib):
    global uuid''')
    src = once(src, '''                    if "V100" in name and int(free_mib) >= 24000 and int(utilization) <= 5 and (
                            device not in excluded):''', '''                    if ("V100" in name and int(free_mib) >= minimum_mib and int(utilization) <= 5
                            and device not in excluded and device not in rejected):''')
    src = once(src, '''                options = []
                for row in rows:''', '''                visible = [row.split(",")[1].strip() for row in rows if "V100" in row]
                if not any(card not in rejected for card in visible):
                    raise RuntimeError("V20 resident startup rejected on every visible V100")
                options = []
                for row in rows:''')
    src = once(src, '''    global drive
    env_path''', '''    global drive, prepared_gpu
    overall_started = time.perf_counter_ns()
    env_path''')
    src = once(src, '''    locks = lease_available_device()
    ipc_parent = None''', '''    validate_and_start = v20handoff.start
    locks, prepared_gpu = validate_and_start(
        globals(), env, shape, fields, java, classpath)
    ipc_parent = None''')
    src = once(src, '''        result = drive(java, classpath, env, feature, shape, fields)
''', '''        result = drive(java, classpath, env, feature, shape, fields)
        result["resident_preflight_seconds"] = prepared_gpu["startup_ns"] / 1e9
        result["end_to_end_with_preparation_seconds"] = (time.perf_counter_ns() - overall_started) / 1e9
''')
    return src

