import os
import base64
import io
import pathlib
import subprocess
import tarfile

src = pathlib.Path(__file__).resolve().parent
DEPLOY_FILES = (
    "fides_gpu.h",
    "fides_gpu.cu",
    "fides_cpu.cpp",
    "FidesNativeCpu.java",
    "F4BaselineDcpCheck.java",
    "v66_patch.py",
    "v67_patch.py",
    "v73_patch.py",
    "prepare_v73.py",
    "record_rounds_v73.py",
)

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tf:
    for name in DEPLOY_FILES:
        p = src / name
        if not p.is_file():
            raise FileNotFoundError(f"Missing deploy file: {p}")
        tf.add(p, arcname=name)
b64 = base64.b64encode(buf.getvalue()).decode("ascii")

remote = f"""ssh gpu02 'bash -s' <<'EOF'
set -euo pipefail
ROOT=/home/yxwang1/FIDES/aquarius-evidence-2026-10-22/ppt-reproduction-2026-11-07/f4-ppt-runtime-measurement-v1
V33=$ROOT/theia50per-ppt-hybrid-v33
V37=$ROOT/theia50per-ppt-hybrid-v37
V73=$ROOT/theia50per-ppt-hybrid-v73
if [ -e "$V73/launch_receipt.json" ] || [ -e "$V73/status.json" ]; then echo "V73_ALREADY_LAUNCHED"; exit 1; fi
rm -rf "$V73"
mkdir -p "$V73"
base64 -d <<'TARB64' | tar -xzf - -C "$V73"
{b64}
TARB64
cp -a "$V33/v33_patch.py" "$V33/v27_patch.py" "$V33/v24_patch.py" "$V73/"
cp -a "$V37/rrg.idmap.bin" "$V73/rrg.idmap.bin"
chmod +x "$V73/record_rounds_v73.py"
cd "$V73"
python3 -B "$V73/prepare_v73.py" --compile-only
echo "=== COMPILE LOGS ==="
ls -lh "$V73/compile_gpu_obj.log" "$V73/compile_cpp.log" "$V73/compile_cuda.log" "$V73/compile_java.log" "$V73/rrg.idmap.bin"
cat "$V73/compile_gpu_obj.log" "$V73/compile_cpp.log" "$V73/compile_java.log"
cat "$V73/prepare_status.json"
EOF
"""

p = subprocess.run(
    ["ssh", os.environ.get("FIDES_SSH_JUMP", "user@login-node"), "-p", os.environ.get("FIDES_SSH_PORT", "22"), "tr -d '\\r' | bash -s"],
    input=remote,
    check=False,
    text=True,
)
raise SystemExit(p.returncode)
