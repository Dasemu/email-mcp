# email-mcp

An MCP server that exposes IMAP/SMTP email operations (list accounts and folders,
read/move/delete emails, send mail) as tools for MCP clients.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example email-mcp.env   # fill in real values
set -a; source email-mcp.env; set +a
python server.py
```

See `CLAUDE.md` for architecture and conventions, and `docs/` for further documentation.
