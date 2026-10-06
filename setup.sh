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

echo "==> Python environment for the VectCutAPI backend (built aside in venv.new)"
cd "$KIT/vectcut-api"
rm -rf venv.new
python3 -m venv venv.new
./venv.new/bin/python -m pip install -q --upgrade pip
./venv.new/bin/python -m pip install -q -r requirements.lock.txt
echo "==> Speech transcription (Whisper)"
if [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
  ./venv.new/bin/python -m pip install -q -r requirements-whisper-mlx.lock.txt   # Apple Silicon GPU
  whisper_module=mlx_whisper
else
  ./venv.new/bin/python -m pip install -q "faster-whisper>=1.0,<2"               # CPU (not version-locked)
  whisper_module=faster_whisper
fi
./venv.new/bin/python -c "import flask, imageio, requests, json5, $whisper_module" || {
  echo "The new Python environment does not work; the previous one was kept."; rm -rf venv.new; exit 1; }
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
  rm -rf .build "$KIT/vectcut-api/venv.new"; exit 1; }

echo "==> Switching to the new build"
cd "$KIT/vectcut-api"
rm -rf venv.old
[[ -d venv ]] && mv venv venv.old
mv venv.new venv
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
# on disk); the MCP server starts the new one on its next call.
pid=$(curl -s --max-time 2 "http://127.0.0.1:$PORT/health" | \
  python3 -c 'import sys, json; o = json.load(sys.stdin)["output"]; print(o["pid"] if o.get("service") == "capcut-mcp-kit" else "")' 2>/dev/null || true)
if [[ -n "$pid" ]]; then
  echo "==> Stopping the running backend (pid $pid); it restarts on the next tool call"
  kill "$pid" 2>/dev/null || true
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
