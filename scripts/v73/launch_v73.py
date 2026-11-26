import os
import subprocess

remote = """ssh gpu02 'bash -s' <<'EOF'
set -euo pipefail
V73=/home/yxwang1/FIDES/aquarius-evidence-2026-10-22/ppt-reproduction-2026-11-07/f4-ppt-runtime-measurement-v1/theia50per-ppt-hybrid-v73
cd "$V73"
echo "=== GPU STATUS BEFORE LAUNCH ==="
nvidia-smi --query-gpu=index,name,memory.used,memory.free,utilization.gpu --format=csv
python3 -B -c 'import prepare_v73 as p, json; pins = json.load(open("corridor_build_receipt.json"))["sha256"]; p.launch(pins)'
nohup python3 -B "$V73/record_rounds_v73.py" 10 6 > "$V73/recorder.log" 2>&1 < /dev/null &
sleep 3
echo "=== LAUNCH RECEIPT ==="
cat "$V73/launch_receipt.json"
echo "=== STATUS.JSON ==="
cat "$V73/status.json" || true
echo "=== LIVE PROCESSES ==="
ps -ef | grep -E "theia50per-ppt-hybrid-v73|record_rounds_v73" | grep -v grep
EOF
"""

p = subprocess.run(
    ["ssh", os.environ.get("FIDES_SSH_JUMP", "user@login-node"), "-p", os.environ.get("FIDES_SSH_PORT", "22"), "tr -d '\\r' | bash -s"],
    input=remote,
    check=False,
    text=True,
)
raise SystemExit(p.returncode)
