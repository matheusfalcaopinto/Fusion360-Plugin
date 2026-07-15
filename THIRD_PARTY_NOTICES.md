# Optional third-party integrations

Fusion Agent Codex does not vendor the following projects. They are installed
only when an operator explicitly selects the corresponding backend.

## Faust Fusion360 MCP Server

- Project: `faust-machines/fusion360-mcp-server`
- Package: `fusion360-mcp-server==0.1.0`
- License: MIT
- Source: <https://github.com/faust-machines/fusion360-mcp-server>
- Purpose: optional persistent stdio MCP backend for Fusion Personal users.

The Fusion Agent façade allowlists typed tools and does not expose Faust's
arbitrary `execute_code` escape hatch.
