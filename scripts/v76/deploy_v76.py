import base64
import io
import os
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
    "v76_patch.py",
    "prepare_v76.py",
    "record_rounds_v76.py",
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
V76=$ROOT/theia50per-ppt-hybrid-v76
if [ -e "$V76/launch_receipt.json" ] || [ -e "$V76/status.json" ]; then echo "V76_ALREADY_LAUNCHED"; exit 1; fi
rm -rf "$V76"
mkdir -p "$V76"
base64 -d <<'TARB64' | tar -xzf - -C "$V76"
{b64}
TARB64
cp -a "$V33/v33_patch.py" "$V33/v27_patch.py" "$V33/v24_patch.py" "$V76/"
cp -a "$V37/rrg.idmap.bin" "$V76/rrg.idmap.bin"
chmod +x "$V76/record_rounds_v76.py"
cd "$V76"
python3 -B "$V76/prepare_v76.py" --compile-only
echo "=== COMPILE LOGS ==="
ls -lh "$V76/compile_gpu_obj.log" "$V76/compile_cpp.log" "$V76/compile_cuda.log" "$V76/compile_java.log" "$V76/rrg.idmap.bin"
cat "$V76/compile_gpu_obj.log" "$V76/compile_cpp.log" "$V76/compile_java.log"
cat "$V76/prepare_status.json"
EOF
"""

p = subprocess.run(
    ["ssh", os.environ.get("FIDES_SSH_JUMP", "user@login-node"), "-p", os.environ.get("FIDES_SSH_PORT", "22"), "tr -d '\\r' | bash -s"],
    input=remote,
    check=False,
    text=True,
)
raise SystemExit(p.returncode)
