# capcut-mcp-server (capcut-mcp-kit edition)

MCP server exposing CapCut editing tools on top of the bundled VectCutAPI backend.

This is a modified copy of [Atx-Guy/capcut-mcp-server](https://github.com/Atx-Guy/capcut-mcp-server).
Installation, tools and usage are documented in the [repository README](../README.md);
the list of changes is in [NOTICE](../NOTICE).

```bash
npm install && npm run build   # setup.sh at the repo root does this for you
node dist/index.js             # stdio transport (default)
TRANSPORT=http PORT=3000 node dist/index.js
```

`CAPCUT_API_URL` sets the backend address (default `http://localhost:9001`).
