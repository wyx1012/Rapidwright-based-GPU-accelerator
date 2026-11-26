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
    "prepare_v70.py",
    "record_rounds_v70.py",
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
V70=$ROOT/theia50per-ppt-hybrid-v70
if [ -e "$V70/launch_receipt.json" ] || [ -e "$V70/status.json" ]; then echo "V70_ALREADY_LAUNCHED"; exit 1; fi
rm -rf "$V70"
mkdir -p "$V70"
base64 -d <<'TARB64' | tar -xzf - -C "$V70"
{b64}
TARB64
cp -a "$V33/v33_patch.py" "$V33/v27_patch.py" "$V33/v24_patch.py" "$V70/"
cp -a "$V37/rrg.idmap.bin" "$V70/rrg.idmap.bin"
chmod +x "$V70/record_rounds_v70.py"
cd "$V70"
python3 -B "$V70/prepare_v70.py" --compile-only
echo "=== COMPILE LOGS ==="
ls -lh "$V70/compile_gpu_obj.log" "$V70/compile_cpp.log" "$V70/compile_cuda.log" "$V70/compile_java.log" "$V70/rrg.idmap.bin"
cat "$V70/compile_gpu_obj.log" "$V70/compile_cpp.log" "$V70/compile_java.log"
cat "$V70/prepare_status.json"
EOF
"""

import os
p = subprocess.run(
    ["ssh", os.environ.get("FIDES_SSH_JUMP", "user@login-node"), "-p", os.environ.get("FIDES_SSH_PORT", "22"), "tr -d '\\r' | bash -s"],
    input=remote,
    check=False,
    text=True,
)
raise SystemExit(p.returncode)
