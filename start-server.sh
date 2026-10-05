#!/bin/zsh
# Starts the VectCutAPI backend required by the "capcut" MCP server, on http://localhost:9001
cd "$(dirname "$0")/vectcut-api" && exec ./venv/bin/python capcut_server.py
