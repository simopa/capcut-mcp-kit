#!/bin/zsh
# capcut-mcp-kit setup (macOS)
# Builds the VectCutAPI backend and the MCP server, then registers the MCP server with Claude Code.
#
# Usage:
#   ./setup.sh                 register for all your Claude Code projects (user scope)
#   ./setup.sh /path/to/proj   register only for that project (writes /path/to/proj/.mcp.json)
set -e

KIT="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$1"

echo "==> Checking prerequisites"
missing=()
for cmd in node npm python3 ffmpeg; do
  command -v $cmd >/dev/null || missing+=$cmd
done
if (( ${#missing} )); then
  echo "Missing: ${missing[*]}"
  echo "Install Homebrew (https://brew.sh), then:  brew install node python ffmpeg"
  exit 1
fi
[[ -d "$HOME/Movies/CapCut/User Data/Projects/com.lveditor.draft" ]] || \
  echo "WARNING: CapCut drafts folder not found. Install CapCut desktop and open it once."

echo "==> Python environment for the VectCutAPI backend"
cd "$KIT/vectcut-api"
rm -rf venv
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
[[ -f config.json ]] || cp config.json.example config.json

echo "==> Speech transcription (Whisper)"
if [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
  ./venv/bin/pip install -q mlx-whisper      # Apple Silicon GPU
else
  ./venv/bin/pip install -q faster-whisper   # CPU, any platform
fi
echo "    (the Whisper model, ~1.6 GB, downloads on the first transcription)"

echo "==> Building the MCP server"
cd "$KIT/capcut-mcp-server"
rm -rf node_modules dist
npm install --silent
npm run build >/dev/null

chmod +x "$KIT/start-server.sh"

if command -v claude >/dev/null; then
  echo "==> Registering the MCP server with Claude Code"
  if [[ -n "$PROJECT" ]]; then
    cd "$PROJECT"
    claude mcp remove capcut -s project >/dev/null 2>&1 || true
    claude mcp add capcut -s project -e CAPCUT_API_URL=http://localhost:9001 -- node "$KIT/capcut-mcp-server/dist/index.js"
  else
    claude mcp remove capcut -s user >/dev/null 2>&1 || true
    claude mcp add capcut -s user -e CAPCUT_API_URL=http://localhost:9001 -- node "$KIT/capcut-mcp-server/dist/index.js"
  fi
else
  echo "Claude Code not found: register the server manually (see README, 'Other MCP clients')."
fi

echo
echo "Done. To use it:"
echo "  open Claude Code and approve the 'capcut' MCP server"
echo "  (the backend starts automatically; ./start-server.sh runs it by hand)"
