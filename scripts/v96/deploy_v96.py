import base64
import io
import os
import pathlib
import subprocess
import tarfile

src = pathlib.Path(__file__).resolve().parent
repo = src.parent.parent
v89_dir = repo / "scripts/v89"
v91_dir = repo / "scripts/v91"
v92_dir = repo / "scripts/v92"
v93_dir = repo / "scripts/v93"
v94_dir = repo / "scripts/v94"
v95_dir = repo / "scripts/v95"
FILE_MAP = [
    (repo / "src/cpp/fides_gpu.h", "fides_gpu.h"),
    (repo / "src/cpp/fides_gpu.cu", "fides_gpu.cu"),
    (repo / "src/cpp/fides_cpu.cpp", "fides_cpu.cpp"),
    (repo / "src/java/FidesNativeCpu.java", "FidesNativeCpu.java"),
    (repo / "src/java/F4BaselineDcpCheck.java", "F4BaselineDcpCheck.java"),
    (v89_dir / "v76_patch.py", "v76_patch.py"),
    (v89_dir / "v77_patch.py", "v77_patch.py"),
    (v89_dir / "v81_patch.py", "v81_patch.py"),
    (v89_dir / "v89_patch.py", "v89_patch.py"),
    (v91_dir / "v91_patch.py", "v91_patch.py"),
    (v92_dir / "v92_patch.py", "v92_patch.py"),
    (v93_dir / "v93_patch.py", "v93_patch.py"),
    (v94_dir / "v94_patch.py", "v94_patch.py"),
    (v95_dir / "v95_patch.py", "v95_patch.py"),
    (src / "v96_patch.py", "v96_patch.py"),
    (src / "prepare_v96.py", "prepare_v96.py"),
    (v89_dir / "record_rounds_v89.py", "record_rounds_v96.py"),
]

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tf:
    for p, arc in FILE_MAP:
        if not p.is_file():
            raise FileNotFoundError(f"Missing deploy file: {p}")
        tf.add(p, arcname=arc)
b64 = base64.b64encode(buf.getvalue()).decode("ascii")

remote = f"""ssh gpu02 'bash -s' <<'EOF'
set -euo pipefail
ROOT=/home/yxwang1/FIDES/aquarius-evidence-2026-10-22/ppt-reproduction-2026-11-07/f4-ppt-runtime-measurement-v1
V76=$ROOT/theia50per-ppt-hybrid-v76
V96=$ROOT/theia50per-ppt-hybrid-v96
if [ -e "$V96/launch_receipt.json" ] || [ -e "$V96/status.json" ]; then echo "V96_ALREADY_LAUNCHED"; exit 1; fi
rm -rf "$V96"
mkdir -p "$V96"
base64 -d <<'TARB64' | tar -xzf - -C "$V96"
{b64}
TARB64
cp -a "$V76/v73_patch.py" "$V76/v67_patch.py" "$V76/v66_patch.py" "$V76/v33_patch.py" "$V76/v27_patch.py" "$V76/v24_patch.py" "$V96/"
cp -a "$V76/rrg.idmap.bin" "$V96/rrg.idmap.bin"
chmod +x "$V96/record_rounds_v96.py"
cd "$V96"
python3 -B "$V96/prepare_v96.py" --compile-only
echo "=== COMPILE LOGS ==="
ls -lh "$V96/compile_gpu_obj.log" "$V96/compile_cpp.log" "$V96/compile_cuda.log" "$V96/compile_java.log" "$V96/rrg.idmap.bin"
cat "$V96/compile_gpu_obj.log" "$V96/compile_cpp.log" "$V96/compile_java.log"
cat "$V96/prepare_status.json"
EOF
"""

p = subprocess.run(
    ["ssh", os.environ.get("FIDES_SSH_JUMP", "yxwang1@10.134.142.143"), "-p", os.environ.get("FIDES_SSH_PORT", "6801"), "tr -d '\\r' | bash -s"],
    input=remote,
    check=False,
    text=True,
)
raise SystemExit(p.returncode)
