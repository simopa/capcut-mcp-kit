#!/bin/zsh
# capcut-mcp-kit setup (macOS)
# Builds the VectCutAPI backend and the MCP server, then registers the MCP server with Claude Code.
# Safe to run again: everything is built aside and swapped in only once it works, so a failed run
# leaves the previous installation as it was. Python packages are installed at the exact versions
# in vectcut-api/requirements*.lock.txt, Node packages from package-lock.json (npm ci).
#
# Usage:
#   ./setup.sh                 register for all your Claude Code projects (user scope)
#   ./setup.sh /path/to/proj   register only for that project (writes /path/to/proj/.mcp.json)
set -e

KIT="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$1"
PORT=9001

echo "==> Checking prerequisites"
missing=()
for cmd in node npm python3 ffmpeg ffprobe; do
  command -v $cmd >/dev/null || missing+=$cmd
done
if (( ${#missing} )); then
  echo "Missing: ${missing[*]}"
  echo "Install Homebrew (https://brew.sh), then:  brew install node python ffmpeg"
  exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10 or newer is needed (found $(python3 --version))"; exit 1; }
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' || {
  echo "Node 18 or newer is needed (found $(node --version))"; exit 1; }
[[ -d "$HOME/Movies/CapCut/User Data/Projects/com.lveditor.draft" ]] || \
  echo "WARNING: CapCut drafts folder not found. Install CapCut desktop and open it once."

echo "==> Python environment for the VectCutAPI backend (built aside in venvs/)"
# Each environment is built at its final path, vectcut-api/venvs/<timestamp>, because pip writes
# that absolute path into the scripts it installs (bin/pip...). vectcut-api/venv is a symlink to the
# active one, swapped atomically once the new one works.
cd "$KIT/vectcut-api"
[[ ! -e venv && -d venv.old ]] && mv venv.old venv   # a run interrupted while switching
active="$PWD/venv"
for d in venvs/*(N); do [[ "${d:A}" == "${active:A}" ]] || rm -rf "$d"; done   # leftovers
rm -rf venv.new venv.old venv.next
mkdir -p venvs
env_dir="venvs/$(date +%Y%m%d-%H%M%S)-$$"
trap '[[ -n "$env_dir" ]] && rm -rf "$KIT/vectcut-api/$env_dir" "$KIT/vectcut-api/venv.next"' EXIT
python3 -m venv "$env_dir"
"$env_dir/bin/python" -m pip install -q --upgrade pip
"$env_dir/bin/python" -m pip install -q -r requirements.lock.txt
echo "==> Speech transcription (Whisper)"
if [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
  "$env_dir/bin/python" -m pip install -q -r requirements-whisper-mlx.lock.txt   # Apple Silicon GPU
  whisper_module=mlx_whisper
else
  "$env_dir/bin/python" -m pip install -q "faster-whisper>=1.0,<2"               # CPU (not version-locked)
  whisper_module=faster_whisper
fi
# Checked the way it will be used: through a symlink, scripts included
ln -s "$env_dir" venv.next
{ ./venv.next/bin/pip --version && \
  ./venv.next/bin/python -c "import flask, imageio, requests, json5, $whisper_module"; } >/dev/null || {
  echo "The new Python environment does not work; the previous one was kept."; exit 1; }
echo "    (the Whisper model, ~1.6 GB, downloads on the first transcription)"
[[ -f config.json ]] || cp config.json.example config.json

echo "==> Building the MCP server (aside in .build)"
cd "$KIT/capcut-mcp-server"
rm -rf .build
mkdir .build
cp package.json package-lock.json tsconfig.json .build/
cp -R src .build/
(cd .build && npm ci --silent --ignore-scripts && npx tsc && chmod +x dist/index.js) || {
  echo "The MCP server did not build; the previous installation was kept."
  rm -rf .build; exit 1; }

echo "==> Switching to the new build"
cd "$KIT/vectcut-api"
# rename(2) swaps the symlink in one step (mv would move venv.next into the directory venv points to);
# a venv that is still a real directory (older setup.sh) is first moved aside.
[[ -d venv && ! -L venv ]] && mv venv venv.old
python3 -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' venv.next venv || {
  [[ ! -e venv && -d venv.old ]] && mv venv.old venv
  echo "Could not switch to the new Python environment; the previous one was kept."; exit 1; }
env_dir=""   # in use now: keep it
for d in venvs/*(N); do [[ "${d:A}" == "${active:A}" ]] || rm -rf "$d"; done   # the previous ones
rm -rf venv.old
cd "$KIT/capcut-mcp-server"
rm -rf node_modules.old dist.old
[[ -d node_modules ]] && mv node_modules node_modules.old
[[ -d dist ]] && mv dist dist.old
mv .build/node_modules node_modules
mv .build/dist dist
rm -rf node_modules.old dist.old .build

chmod +x "$KIT/start-server.sh"

# A backend still running from the old environment would keep old code: stop it (drafts are saved
# on disk); the MCP server starts the new one on its next call. /health says who answers but anyone
# can claim it, so its pid is stopped only if that process is this kit's backend: it listens on
# $PORT, it is yours, and it runs the kit's Python on capcut_server.py in $KIT/vectcut-api (as
# start-server.sh and the MCP server start it).
is_kit_backend() {
  local p=$1
  lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null | grep -qx "$p" || return 1
  [[ "${$(ps -o uid= -p $p 2>/dev/null)// /}" == "$UID" ]] || return 1
  python3 - "$KIT/vectcut-api" "$(lsof -a -p $p -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" \
    "$(ps -ww -o command= -p $p 2>/dev/null)" <<'EOF'
import os, re, sys
api, cwd, cmd = sys.argv[1:]
real = os.path.realpath
def kit_python(path):  # <api>/venv/bin/python* or <api>/venvs/<name>/bin/python*
    path = os.path.normpath(os.path.join(cwd, path))
    bin_dir, exe = os.path.split(path)
    env, home = os.path.dirname(bin_dir), os.path.dirname(os.path.dirname(bin_dir))
    if not re.fullmatch(r"python(3(\.\d+)?)?", exe) or os.path.basename(bin_dir) != "bin":
        return False
    if os.path.basename(env) == "venv" and real(os.path.dirname(env)) == real(api):
        return True
    return os.path.basename(home) == "venvs" and real(os.path.dirname(home)) == real(api)
ok = bool(cwd) and real(cwd) == real(api) and any(
    real(os.path.join(cwd, cmd[i + 1:])) == real(os.path.join(api, "capcut_server.py"))
    and kit_python(cmd[:i]) for i in range(len(cmd)) if cmd[i] == " ")
sys.exit(0 if ok else 1)
EOF
}
pid=$(curl -s --max-time 2 "http://127.0.0.1:$PORT/health" | \
  python3 -c 'import sys, json; o = json.load(sys.stdin)["output"]; print(o["pid"] if o.get("service") == "capcut-mcp-kit" else "")' 2>/dev/null || true)
if [[ "$pid" == <-> ]]; then
  if is_kit_backend "$pid"; then
    echo "==> Stopping the running backend (pid $pid); it restarts on the next tool call"
    kill "$pid" 2>/dev/null || true
  else
    echo "NOTE: a program on port $PORT says it is the kit's backend (pid $pid) but could not be confirmed"
    echo "      as such, so it was left running. If it is, stop it yourself: the next tool call starts"
    echo "      the new build."
  fi
fi

if command -v claude >/dev/null; then
  echo "==> Registering the MCP server with Claude Code"
  if [[ -n "$PROJECT" ]]; then
    cd "$PROJECT"
    claude mcp remove capcut -s project >/dev/null 2>&1 || true
    claude mcp add capcut -s project -e CAPCUT_API_URL=http://localhost:$PORT -- node "$KIT/capcut-mcp-server/dist/index.js"
  else
    claude mcp remove capcut -s user >/dev/null 2>&1 || true
    claude mcp add capcut -s user -e CAPCUT_API_URL=http://localhost:$PORT -- node "$KIT/capcut-mcp-server/dist/index.js"
  fi
else
  echo "Claude Code not found: register the server manually (see README, 'Other MCP clients')."
fi

echo
echo "Done. To use it:"
echo "  open Claude Code and approve the 'capcut' MCP server"
echo "  (the backend starts automatically; ./start-server.sh runs it by hand)"
