# CLAUDE.md

## Overview
A single-file MCP (Model Context Protocol) server exposing IMAP/SMTP email operations
(list/read/move/delete emails, manage folders, send mail) as MCP tools over
streamable-http, for one or more configured email accounts.

## Commands
- Run locally: `python server.py` (requires env vars below; see `.env.example`).
- Install dependencies: `pip install -r requirements.txt`.
- Test: no test suite yet.
- Lint/format: no linter/formatter configured yet.
- Build: none (plain Python script, deployed via Nixpacks — see `nixpacks.toml`).

## Architecture
Everything lives in `server.py`: env-based config, IMAP/SMTP helper functions, and
`@mcp.tool()`-decorated functions exposed to MCP clients. See `docs/` for any deeper
notes as they are added.

## Conventions
- Python 3, stdlib `imaplib`/`smtplib`/`email` for all mail access — no mail client
  framework.
- Tools return plain strings or JSON-serializable dicts/lists, never raw IMAP data.

## Workflow quirks
- `EMAIL_ACCOUNTS` is a JSON object mapping account (email address) to password; it is
  read from the environment, never from a committed file.

## Deployment
Deployed via Nixpacks (`nixpacks.toml`: `python server.py`) to a PaaS-style host, not
the LXC/systemd pattern. The server binds to `BIND_HOST`/`BIND_PORT` and speaks MCP
over streamable-http.

## Gotchas
- `purge_email` permanently deletes a message; there is no undo.
- Real credentials belong in `email-mcp.env` (gitignored), never in `.env.example` or
  in code.
