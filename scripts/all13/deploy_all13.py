"""Upload run_all13.py to gpu02 and (optionally) start it detached.

Env: FIDES_SSH_JUMP (user@login-node), FIDES_SSH_PORT.
Usage: deploy_all13.py [--engine v76] [--force] [--timeout SEC] [case ...]   (no case = upload only)
"""
import argparse
import base64
import os
import pathlib
import shlex
import subprocess

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--engine", default="v76")
known, rest = parser.parse_known_args()
engine = known.engine

# Any non-flag positional token in `rest` (not following a value flag) means cases were requested.
value_flags = {"--phase", "--timeout"}
cases_present = False
skip_next = False
for tok in rest:
    if skip_next:
        skip_next = False
        continue
    if tok in value_flags:
        skip_next = True
        continue
    if not tok.startswith("-"):
        cases_present = True
        break

here = pathlib.Path(__file__).resolve().parent
b64 = base64.b64encode((here / "run_all13.py").read_bytes()).decode("ascii")
run_args = " ".join(["--engine", shlex.quote(engine)] + [shlex.quote(a) for a in rest])
launch = ""
if cases_present:
    launch = f"""cd "$D"
nohup python3 -B "$D/run_all13.py" {run_args} > "$D/driver_$(date +%Y%m%d_%H%M%S).log" 2>&1 < /dev/null &
sleep 2
echo DRIVER_STARTED; ps -ef | grep run_all13 | grep -v grep"""
remote = f"""ssh gpu02 'bash -s' <<'EOF'
set -euo pipefail
D=/home/yxwang1/FIDES/aquarius-evidence-2026-10-22/ppt-reproduction-2026-11-07/f4-ppt-runtime-measurement-v1/{engine}all13-driver
mkdir -p "$D"
base64 -d > "$D/run_all13.py" <<'B64'
{b64}
B64
python3 -m py_compile "$D/run_all13.py" && echo UPLOAD_OK
{launch}
EOF
"""
p = subprocess.run(
    ["ssh", os.environ.get("FIDES_SSH_JUMP", "user@login-node"), "-p", os.environ.get("FIDES_SSH_PORT", "22"),
     "tr -d '\\r' | bash -s"],
    input=remote, check=False, text=True)
raise SystemExit(p.returncode)
