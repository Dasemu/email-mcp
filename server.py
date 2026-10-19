import base64
import email
import email.header
import imaplib
import json
import os
import re
import smtplib
from contextlib import contextmanager
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from mcp.server.mcpserver import MCPServer

IMAP_HOST = os.environ["IMAP_HOST"]
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
ACCOUNTS = json.loads(os.environ["EMAIL_ACCOUNTS"])
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("BIND_PORT", "8084"))

mcp = MCPServer("email")


def _password(account: str) -> str:
    if account not in ACCOUNTS:
        raise ValueError(f"Unknown account {account!r}. Use list_accounts to see available accounts.")
    return ACCOUNTS[account]


@contextmanager
def _imap(account: str):
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
    try:
        conn.login(account, _password(account))
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _decode_header_value(value) -> str | None:
    if not value:
        return None
    parts = email.header.decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _extract_flags(meta: bytes) -> list[str]:
    m = re.search(rb"FLAGS \(([^)]*)\)", meta)
    return m.group(1).decode().split() if m else []


def _parse_folder_line(line: str) -> dict | None:
    m = re.match(r'\(([^)]*)\)\s+"([^"]*)"\s+(.+)', line)
    if not m:
        return None
    flags_raw, delim, name = m.groups()
    name = name.strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return {"flags": flags_raw.split(), "delimiter": delim, "name": name}


def _folders(conn) -> list[dict]:
    typ, data = conn.list()
    if typ != "OK":
        raise RuntimeError(f"LIST failed: {data}")
    out = []
    for raw in data:
        parsed = _parse_folder_line(raw.decode(errors="replace"))
        if parsed:
            out.append(parsed)
    return out


def _special_folder(folders: list[dict], flag: str, fallback: str) -> str:
    for f in folders:
        if flag in f["flags"]:
            return f["name"]
    return fallback


def _move(conn, uid: str, dest_folder: str) -> None:
    try:
        typ, data = conn.uid("MOVE", uid, f'"{dest_folder}"')
    except imaplib.IMAP4.error:
        typ = "NO"
    if typ == "OK":
        return
    typ, data = conn.uid("COPY", uid, f'"{dest_folder}"')
    if typ != "OK":
        raise RuntimeError(f"Could not move message {uid} to {dest_folder}: {data}")
    conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
    conn.expunge()


def _fetch_raw(conn, uid: str) -> bytes:
    typ, data = conn.uid("fetch", uid, "(BODY.PEEK[])")
    for part in data:
        if isinstance(part, tuple):
            return part[1]
    raise ValueError(f"Email {uid} not found")


@mcp.tool()
def list_accounts() -> list[str]:
    """List configured email accounts this MCP can access."""
    return sorted(ACCOUNTS.keys())


@mcp.tool()
def list_folders(account: str) -> list[dict]:
    """List IMAP folders/mailboxes for an email account."""
    with _imap(account) as conn:
        return [{"name": f["name"], "flags": f["flags"], "delimiter": f["delimiter"]}
                 for f in _folders(conn)]


@mcp.tool()
def create_folder(account: str, folder: str) -> str:
    """Create a new IMAP folder/mailbox. For a nested folder, use the account's
    hierarchy delimiter (check list_folders' `delimiter`, commonly '/' or '.'),
    e.g. 'Projects/Personal'."""
    with _imap(account) as conn:
        typ, data = conn.create(f'"{folder}"')
        if typ != "OK":
            raise RuntimeError(f"Could not create folder {folder!r}: {data}")
    return f"Created folder {folder}"


@mcp.tool()
def list_emails(account: str, folder: str = "INBOX", limit: int = 20,
                 unread_only: bool = False) -> list[dict]:
    """List recent emails in a folder, newest first (uid, from, subject, date, seen)."""
    with _imap(account) as conn:
        conn.select(f'"{folder}"', readonly=True)
        typ, data = conn.uid("search", None, "(UNSEEN)" if unread_only else "ALL")
        uids = data[0].split()[-limit:][::-1]
        results = []
        for uid in uids:
            typ, msg_data = conn.uid("fetch", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            headers, flags = {}, []
            for part in msg_data:
                if isinstance(part, tuple):
                    meta, raw = part
                    flags = _extract_flags(meta)
                    msg = email.message_from_bytes(raw)
                    headers = {"from": _decode_header_value(msg.get("From")),
                               "subject": _decode_header_value(msg.get("Subject")),
                               "date": msg.get("Date")}
            results.append({"uid": uid.decode(), **headers, "seen": "\\Seen" in flags})
        return results


@mcp.tool()
def search_emails(account: str, query: str, folder: str = "INBOX", limit: int = 20) -> list[dict]:
    """Search emails in a folder by text (matches subject and body). Returns newest first."""
    with _imap(account) as conn:
        conn.select(f'"{folder}"', readonly=True)
        typ, data = conn.uid("search", "UTF-8", "TEXT", f'"{query}"')
        uids = data[0].split()[-limit:][::-1]
        results = []
        for uid in uids:
            typ, msg_data = conn.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            for part in msg_data:
                if isinstance(part, tuple):
                    msg = email.message_from_bytes(part[1])
                    results.append({"uid": uid.decode(),
                                     "from": _decode_header_value(msg.get("From")),
                                     "subject": _decode_header_value(msg.get("Subject")),
                                     "date": msg.get("Date")})
        return results


@mcp.tool()
def get_email(account: str, folder: str, uid: str) -> dict:
    """Get full details of an email: from/to/cc/subject/date, plain-text body, and a
    list of attachment filenames (use get_attachment to fetch attachment content)."""
    with _imap(account) as conn:
        conn.select(f'"{folder}"', readonly=True)
        msg = email.message_from_bytes(_fetch_raw(conn, uid))
    body_text = None
    attachments = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        disp = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()
        if filename:
            attachments.append(_decode_header_value(filename))
        elif part.get_content_type() == "text/plain" and "attachment" not in disp and body_text is None:
            payload = part.get_payload(decode=True)
            if payload is not None:
                body_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return {
        "uid": uid,
        "from": _decode_header_value(msg.get("From")),
        "to": _decode_header_value(msg.get("To")),
        "cc": _decode_header_value(msg.get("Cc")),
        "subject": _decode_header_value(msg.get("Subject")),
        "date": msg.get("Date"),
        "body": body_text,
        "attachments": attachments,
    }


@mcp.tool()
def get_attachment(account: str, folder: str, uid: str, filename: str,
                    max_bytes: int = 5_000_000) -> dict:
    """Fetch an email attachment's content, base64-encoded. Fails if it exceeds max_bytes."""
    with _imap(account) as conn:
        conn.select(f'"{folder}"', readonly=True)
        msg = email.message_from_bytes(_fetch_raw(conn, uid))
    for part in msg.walk():
        fname = part.get_filename()
        if fname and _decode_header_value(fname) == filename:
            payload = part.get_payload(decode=True)
            if len(payload) > max_bytes:
                raise ValueError(f"Attachment exceeds max_bytes ({len(payload)} > {max_bytes})")
            return {"filename": filename, "content_type": part.get_content_type(),
                     "base64": base64.b64encode(payload).decode("ascii")}
    raise ValueError(f"Attachment {filename!r} not found on email {uid}")


@mcp.tool()
def mark_read(account: str, folder: str, uid: str) -> str:
    """Mark an email as read."""
    with _imap(account) as conn:
        conn.select(f'"{folder}"')
        conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
    return f"Marked {uid} as read"


@mcp.tool()
def mark_unread(account: str, folder: str, uid: str) -> str:
    """Mark an email as unread."""
    with _imap(account) as conn:
        conn.select(f'"{folder}"')
        conn.uid("STORE", uid, "-FLAGS", "(\\Seen)")
    return f"Marked {uid} as unread"


@mcp.tool()
def move_email(account: str, folder: str, uid: str, destination_folder: str) -> str:
    """Move an email to a different folder."""
    with _imap(account) as conn:
        conn.select(f'"{folder}"')
        _move(conn, uid, destination_folder)
    return f"Moved {uid} to {destination_folder}"


@mcp.tool()
def delete_email(account: str, folder: str, uid: str) -> str:
    """Delete an email (moves it to the Trash folder). Use purge_email to permanently
    remove a message instead."""
    with _imap(account) as conn:
        conn.select(f'"{folder}"')
        trash = _special_folder(_folders(conn), "\\Trash", "Trash")
        _move(conn, uid, trash)
    return f"Moved {uid} to {trash}"


@mcp.tool()
def purge_email(account: str, folder: str, uid: str) -> str:
    """Permanently delete an email. This cannot be undone."""
    with _imap(account) as conn:
        conn.select(f'"{folder}"')
        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()
    return f"Permanently deleted {uid}"


@mcp.tool()
def send_email(account: str, to: str, subject: str, body: str,
                cc: str | None = None, bcc: str | None = None) -> str:
    """Send a plain-text email from the given account. to/cc/bcc accept comma-separated
    addresses."""
    msg = MIMEMultipart()
    msg["From"] = account
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    msg.attach(MIMEText(body, "plain", "utf-8"))
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    if cc:
        recipients += [a.strip() for a in cc.split(",") if a.strip()]
    if bcc:
        recipients += [a.strip() for a in bcc.split(",") if a.strip()]
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.login(account, _password(account))
        s.sendmail(account, recipients, msg.as_string())
    return f"Sent to {', '.join(recipients)}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=BIND_HOST, port=BIND_PORT)
