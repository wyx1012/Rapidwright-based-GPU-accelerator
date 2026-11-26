"""Attach the measured THEIA feature kernel to the GPU-only router's live startup."""
from pathlib import Path
import shutil
import subprocess


FEATURE_COUNT = 4_952_719


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("Unexpected live-feature anchor: " + old.splitlines()[0])
    return source.replace(old, new, 1)


def patch_router(source):
    anchor = "        if (F4DirectGpu.enabled()) f4PrecomputeDirect();"
    return replace_once(source, anchor,
                        "        F2Prewarm.initialize(indirectConnections, directConnections);\n" + anchor)


def prepare_sources(project, work, nvcc, env):
    """Generate two overlay Java sources and compile the feature cubin on gpu02."""
    project, work = Path(project), Path(work)
    upstream = project / "f2-context-prewarm-v1"
    source = (upstream / "F2Prewarm.java").read_text()
    source = replace_once(source, "indirectCount != 29451 || all.size() != 29524",
                          f"indirectCount != 4_909_155 || all.size() != {FEATURE_COUNT:_}")
    source = replace_once(source, "Pointer.to(new long[]{150}),Pointer.to(new long[]{40})",
                          "Pointer.to(new long[]{-1L}),Pointer.to(new long[]{-1L})")
    # Do not retain a duplicate 178 MiB input or materialize an additional output array.
    old = """            ByteBuffer rawInput = ByteBuffer.allocate(input.length * Integer.BYTES).order(ByteOrder.LITTLE_ENDIAN);
            rawInput.asIntBuffer().put(input);
            ByteBuffer rawOutput = ByteBuffer.allocate(result.words.length * Long.BYTES).order(ByteOrder.LITTLE_ENDIAN);
            rawOutput.asLongBuffer().put(result.words);
            Path inputFile = Path.of(System.getProperty("fides.f2.input"));
            Path outputFile = Path.of(System.getProperty("fides.f2.table"));
            Files.write(inputFile,rawInput.array(),StandardOpenOption.CREATE_NEW);
            Files.write(outputFile,rawOutput.array(),StandardOpenOption.CREATE_NEW);"""
    new = """            Path outputFile = Path.of(System.getProperty("fides.ppt.f4.features"));
            try (var table = java.nio.channels.FileChannel.open(
                    outputFile, StandardOpenOption.CREATE_NEW, StandardOpenOption.READ, StandardOpenOption.WRITE)) {
                table.map(java.nio.channels.FileChannel.MapMode.READ_WRITE,
                          0, (long) result.words.length * Long.BYTES)
                    .order(ByteOrder.LITTLE_ENDIAN).asLongBuffer().put(result.words);
            }"""
    source = replace_once(source, old, new)
    source = replace_once(source, "import java.nio.ByteBuffer;\n", "")
    (work / "F2Prewarm.java").write_text(source)
    shutil.copyfile(upstream / "PartialRouter.java", work / "PartialRouter.java")
    shutil.copyfile(upstream / "feature_kernel.cu", work / "feature_kernel.cu")
    with (work / "compile_live_feature.log").open("xb") as log:
        subprocess.run([str(nvcc), "-std=c++14", "-O3", "-arch=sm_70", "-cubin",
                        "feature_kernel.cu", "-o", "feature.cubin"], cwd=work, env=env,
                       check=True, stdout=log, stderr=subprocess.STDOUT)
    return "F2Prewarm.java", "PartialRouter.java"


def patch_worker(source):
    feature_anchor = '''f"-Dfides.ppt.f4.features={feature['features_path']}",'''
    with_options = (feature_anchor + ' "-Dfides.f2.prewarm=background", '
                    '''f"-Dfides.f2.cubin={work/'feature.cubin'}",''')
    launch = 'app, native_log = launch("native_multichannel", args, env)'
    visible = ('app, native_log = launch("native_multichannel", args, '
               'env | {"CUDA_VISIBLE_DEVICES": uuid})')
    source = replace_once(source, "top = ast.parse(script)",
                          f"script = swap(script, {feature_anchor!r}, {with_options!r})\n"
                          f"script = swap(script, {launch!r}, {visible!r})\n"
                          "top = ast.parse(script)")
    old = """    feature = read(features_dir / "result.json")
    check(feature["status"] == "passed" and feature["design"] == "THEIA_50per", "Wrong GPU features")"""
    new = """    feature = read(features_dir / "result.json")
    check(feature["status"] == "passed" and feature["design"] == "THEIA_50per"
          and digest(dcp) == feature["input_sha256"]
          and feature["initial_connections"] == 4952719, "Wrong THEIA feature reference or input")
    feature["features_path"] = str(work / "features.live.bin")
    serialized_bytes = dcp.stat().st_size
    resource_tier = "huge" if serialized_bytes >= 192 << 20 else "standard"
    save("live_cuda_feature_planned", feature_reference_sha256=feature["features_sha256"],
         resource_tier=resource_tier, serialized_input_bytes=serialized_bytes)"""
    source = replace_once(source, old, new)
    old = "        result = drive(java, classpath, env, feature, shape, fields)"
    new = """        result = drive(java, classpath, env, feature, shape, fields)
        live_path = Path(feature["features_path"])
        check(live_path.stat().st_size == 4952719 * 72, "Incomplete live GPU feature output")
        live_sha = digest(live_path)
        result["live_features"] = dict(source="routing_jvm_background_cuda", rows=4952719,
            sha256=live_sha, archived_sha256=feature["features_sha256"],
            archive_match=live_sha == feature["features_sha256"], gpu_uuid=uuid,
            serialized_input_bytes=serialized_bytes, resource_tier=resource_tier)"""
    return replace_once(source, old, new)
