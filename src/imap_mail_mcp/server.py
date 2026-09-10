#!/usr/bin/env python3
"""
imap-mail-mcp — Servidor MCP para IMAP/SMTP.

Dependência externa: apenas o SDK `mcp`. Todo o protocolo (IMAP4rev1,
SMTP, MIME) é feito com a biblioteca padrão do Python.

Configuração via variáveis de ambiente (ver README.md).
"""

from __future__ import annotations

import base64
import imaplib
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator, Optional

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


CFG = {
    "imap_host": _env("IMAP_HOST"),
    "imap_port": int(_env("IMAP_PORT", "993")),
    "imap_ssl": _env_bool("IMAP_SSL", True),          # False => STARTTLS na 143
    "smtp_host": _env("SMTP_HOST"),
    "smtp_port": int(_env("SMTP_PORT", "587")),
    "smtp_security": _env("SMTP_SECURITY", "starttls").lower(),  # starttls | ssl | none
    "user": _env("MAIL_USER"),
    "password": _env("MAIL_PASS"),
    "from": _env("MAIL_FROM"),                        # "Nome <email>" ou vazio => MAIL_USER
    "sent_folder": _env("SENT_FOLDER"),               # auto-detecta se vazio
    "attach_dir": _env("ATTACH_DIR", str(Path.home() / "mcp-mail-attachments")),
    "verify_tls": _env_bool("TLS_VERIFY", True),
    "max_body_chars": int(_env("MAX_BODY_CHARS", "20000")),
}

imaplib._MAXLINE = 10_000_000  # anexos grandes nas respostas FETCH


def _require(*keys: str) -> None:
    missing = [k for k in keys if not CFG.get(k)]
    if missing:
        raise RuntimeError(f"Configuração ausente: {', '.join(k.upper() for k in missing)}")


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not CFG["verify_tls"]:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------------------------------------------------------------------------
# Utilidades IMAP
# ---------------------------------------------------------------------------


def imap_utf7_encode(s: str) -> str:
    """Codifica nome de pasta em UTF-7 modificado (RFC 3501 §5.1.3)."""
    if s.isascii() and "&" not in s:
        return s
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            b = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(b).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(s: str) -> str:
    def repl(m: re.Match) -> str:
        t = m.group(1)
        if not t:
            return "&"
        t = t.replace(",", "/")
        t += "=" * (-len(t) % 4)
        return base64.b64decode(t).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", repl, s)


def _quote(folder: str) -> str:
    f = imap_utf7_encode(folder)
    return '"' + f.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _check(res: tuple, action: str) -> Any:
    typ, data = res
    if typ != "OK":
        msg = data[0].decode(errors="replace") if data and isinstance(data[0], bytes) else str(data)
        raise RuntimeError(f"IMAP {action} falhou: {msg}")
    return data


@contextmanager
def imap_conn(folder: Optional[str] = None, readonly: bool = False) -> Iterator[imaplib.IMAP4]:
    _require("imap_host", "user", "password")
    if CFG["imap_ssl"]:
        conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(CFG["imap_host"], CFG["imap_port"], ssl_context=_ssl_ctx())
    else:
        conn = imaplib.IMAP4(CFG["imap_host"], CFG["imap_port"])
        try:
            conn.starttls(_ssl_ctx())
        except Exception:
            pass  # servidor sem STARTTLS
    try:
        conn.login(CFG["user"], CFG["password"])
        try:
            conn.enable("UTF8=ACCEPT")
        except Exception:
            pass
        if folder:
            _check(conn.select(_quote(folder), readonly=readonly), f"SELECT {folder}")
        yield conn
    finally:
        try:
            if conn.state == "SELECTED":
                conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass


_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')


def _parse_list(line: bytes) -> Optional[dict]:
    m = _LIST_RE.match(line)
    if not m:
        return None
    name = m.group("name").decode(errors="replace").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    delim = m.group("delim").decode()
    delim = "" if delim == "NIL" else delim.strip('"')
    flags = [f.decode() for f in m.group("flags").split()]
    return {"name": imap_utf7_decode(name), "raw_name": name, "delimiter": delim, "flags": flags}


def _list_folders(conn: imaplib.IMAP4) -> list[dict]:
    data = _check(conn.list(), "LIST")
    out = []
    for line in data:
        if isinstance(line, tuple):  # nome literal
            line = line[0] + b' "' + line[1] + b'"'
        if line:
            p = _parse_list(line)
            if p:
                out.append(p)
    return out


def _find_sent_folder(conn: imaplib.IMAP4) -> Optional[str]:
    if CFG["sent_folder"]:
        return CFG["sent_folder"]
    folders = _list_folders(conn)
    for f in folders:
        if "\\Sent" in f["flags"]:
            return f["name"]
    names = {f["name"].lower(): f["name"] for f in folders}
    for cand in ("sent", "sent items", "sent messages", "enviados", "itens enviados", "inbox.sent", "[gmail]/sent mail"):
        if cand in names:
            return names[cand]
    return None


def _decode_hdr(v: Any) -> str:
    if v is None:
        return ""
    try:
        return str(make_header(decode_header(str(v))))
    except Exception:
        return str(v)


def _fmt_date(v: Any) -> str:
    try:
        return parsedate_to_datetime(str(v)).isoformat()
    except Exception:
        return str(v or "")


def _imap_date(s: str) -> str:
    """'2026-09-09' -> '09-Sep-2026'"""
    d = datetime.strptime(s, "%Y-%m-%d")
    return d.strftime("%d-%b-%Y")


_FETCH_META_RE = re.compile(rb"UID (\d+)")
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")


def _fetch_summaries(conn: imaplib.IMAP4, uids: list[str]) -> list[dict]:
    if not uids:
        return []
    data = _check(conn.uid("FETCH", ",".join(uids), "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])"), "FETCH")
    out = []
    for item in data:
        if not isinstance(item, tuple):
            continue
        meta, raw = item[0], item[1]
        m_uid = _FETCH_META_RE.search(meta)
        if not m_uid:
            continue
        flags = _FLAGS_RE.search(meta)
        size = _SIZE_RE.search(meta)
        msg = message_from_bytes(raw, policy=policy.default)
        out.append({
            "uid": int(m_uid.group(1)),
            "date": _fmt_date(msg["Date"]),
            "from": _decode_hdr(msg["From"]),
            "to": _decode_hdr(msg["To"]),
            "cc": _decode_hdr(msg["Cc"]),
            "subject": _decode_hdr(msg["Subject"]),
            "message_id": str(msg["Message-ID"] or ""),
            "flags": flags.group(1).decode().split() if flags else [],
            "size": int(size.group(1)) if size else None,
        })
    order = {u: i for i, u in enumerate(uids)}
    out.sort(key=lambda r: order.get(str(r["uid"]), 0))
    return out


def _fetch_raw(conn: imaplib.IMAP4, uid: int, peek: bool = True) -> bytes:
    part = "BODY.PEEK[]" if peek else "BODY[]"
    data = _check(conn.uid("FETCH", str(uid), f"({part})"), "FETCH")
    for item in data:
        if isinstance(item, tuple):
            return item[1]
    raise RuntimeError(f"UID {uid} não encontrado")


def _body_text(msg: EmailMessage, prefer_html: bool) -> tuple[str, str]:
    pref = ("html", "plain") if prefer_html else ("plain", "html")
    part = msg.get_body(preferencelist=pref)
    if part is None:
        return "", ""
    try:
        return part.get_content(), part.get_content_subtype()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace"), part.get_content_subtype()


def _attachments(msg: EmailMessage) -> list[tuple[int, str, str, int]]:
    out = []
    for i, part in enumerate(msg.iter_attachments()):
        name = part.get_filename() or f"anexo-{i}"
        payload = part.get_payload(decode=True) or b""
        out.append((i, _decode_hdr(name), part.get_content_type(), len(payload)))
    return out


def _safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip() or "anexo"
    return name[:200]


def _build_search(unseen: bool, flagged: bool, sender: str, to: str, subject: str, text: str,
                  since: str, before: str, larger_than_kb: int) -> tuple[str, Optional[bytes]]:
    """Retorna (critério, literal). Só um termo não-ASCII pode virar literal."""
    crit: list[str] = []
    literal: Optional[bytes] = None
    if unseen:
        crit.append("UNSEEN")
    if flagged:
        crit.append("FLAGGED")
    if since:
        crit.append(f"SINCE {_imap_date(since)}")
    if before:
        crit.append(f"BEFORE {_imap_date(before)}")
    if larger_than_kb > 0:
        crit.append(f"LARGER {larger_than_kb * 1024}")
    lit_term = ""
    for key, val in (("FROM", sender), ("TO", to), ("SUBJECT", subject), ("TEXT", text)):
        if not val:
            continue
        if val.isascii():
            esc = val.replace("\\", "\\\\").replace('"', '\\"')
            crit.append(f'{key} "{esc}"')
        elif literal is None:
            literal = val.encode("utf-8")
            lit_term = key  # imaplib anexa "{n}" + literal ao final do comando
        else:
            raise ValueError("Apenas um filtro com acentos/não-ASCII por busca é suportado.")
    if lit_term:
        crit.append(lit_term)
    if not crit:
        crit.append("ALL")
    return " ".join(crit), literal


def _search(conn: imaplib.IMAP4, criteria: str, literal: Optional[bytes]) -> list[str]:
    if literal is not None:
        conn.literal = literal
        data = _check(conn.uid("SEARCH", "CHARSET", "UTF-8", criteria), "SEARCH")
    else:
        data = _check(conn.uid("SEARCH", None, criteria), "SEARCH")
    if not data or not data[0]:
        return []
    return data[0].decode().split()


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------


def _from_addr() -> str:
    return CFG["from"] or CFG["user"]


def _split_addrs(s: str) -> list[str]:
    return [a.strip() for a in re.split(r"[;,]", s or "") if a.strip()]


def _compose(to: str, subject: str, body: str, cc: str = "", bcc: str = "", html: bool = False,
             attachments: Optional[list[str]] = None, in_reply_to: str = "",
             references: str = "", reply_to: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = _from_addr()
    msg["To"] = ", ".join(_split_addrs(to))
    if cc:
        msg["Cc"] = ", ".join(_split_addrs(cc))
    if bcc:
        msg["Bcc"] = ", ".join(_split_addrs(bcc))
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=parseaddr(_from_addr())[1].split("@")[-1] or None)
    if reply_to:
        msg["Reply-To"] = reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if html:
        # texto simples como alternativa mínima
        plain = re.sub(r"<[^>]+>", "", body)
        msg.set_content(plain)
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    for path in attachments or []:
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Anexo não encontrado: {p}")
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def _smtp_send(msg: EmailMessage) -> None:
    _require("smtp_host", "user", "password")
    sec = CFG["smtp_security"]
    if sec == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(CFG["smtp_host"], CFG["smtp_port"], context=_ssl_ctx(), timeout=60)
    else:
        server = smtplib.SMTP(CFG["smtp_host"], CFG["smtp_port"], timeout=60)
    with server:
        server.ehlo()
        if sec == "starttls":
            server.starttls(context=_ssl_ctx())
            server.ehlo()
        if sec != "none" or CFG["password"]:
            try:
                server.login(CFG["user"], CFG["password"])
            except smtplib.SMTPNotSupportedError:
                pass
        server.send_message(msg)


def _append_sent(msg: EmailMessage) -> Optional[str]:
    try:
        with imap_conn() as conn:
            folder = _find_sent_folder(conn)
            if not folder:
                return None
            conn.append(_quote(folder), "(\\Seen)", imaplib.Time2Internaldate(datetime.now().timestamp()), msg.as_bytes())
            return folder
    except Exception as e:  # não falha o envio por causa disso
        print(f"[imap-mail] aviso: não foi possível gravar em Enviados: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Servidor MCP
# ---------------------------------------------------------------------------

mcp = _Server(
    "imap-mail",
    instructions=(
        "Cliente de e-mail IMAP/SMTP. Use list_folders para ver pastas, search_emails "
        "para localizar mensagens (retorna UIDs), read_email para ler o conteúdo pelo UID, "
        "send_email/reply_email para enviar. UIDs são estáveis dentro de cada pasta."
    ),
)


@mcp.tool()
def list_folders(with_counts: bool = False) -> list[dict]:
    """Lista todas as pastas (mailboxes) da conta. Com with_counts=True inclui total e não lidos (mais lento)."""
    with imap_conn() as conn:
        folders = _list_folders(conn)
        if with_counts:
            for f in folders:
                if "\\Noselect" in f["flags"]:
                    continue
                try:
                    typ, data = conn.status(_quote(f["name"]), "(MESSAGES UNSEEN)")
                    if typ == "OK" and data and data[0]:
                        s = data[0].decode(errors="replace")
                        m1, m2 = re.search(r"MESSAGES (\d+)", s), re.search(r"UNSEEN (\d+)", s)
                        f["messages"] = int(m1.group(1)) if m1 else None
                        f["unseen"] = int(m2.group(1)) if m2 else None
                except Exception:
                    pass
        for f in folders:
            f.pop("raw_name", None)
        return folders


@mcp.tool()
def search_emails(
    folder: str = "INBOX",
    unseen: bool = False,
    flagged: bool = False,
    sender: str = "",
    to: str = "",
    subject: str = "",
    text: str = "",
    since: str = "",
    before: str = "",
    larger_than_kb: int = 0,
    limit: int = 20,
    raw_criteria: str = "",
) -> dict:
    """Busca e-mails em uma pasta. Retorna os mais recentes primeiro com UID, remetente, assunto, data e flags.
    Datas no formato YYYY-MM-DD. raw_criteria permite passar critério IMAP SEARCH direto (ex.: 'UNSEEN FROM "x"'), ignorando os demais filtros.
    Os filtros sender/to/subject/text são substring, case-insensitive."""
    with imap_conn(folder, readonly=True) as conn:
        if raw_criteria:
            uids = _search(conn, raw_criteria, None)
        else:
            crit, lit = _build_search(unseen, flagged, sender, to, subject, text, since, before, larger_than_kb)
            uids = _search(conn, crit, lit)
        total = len(uids)
        uids = uids[-max(1, min(limit, 200)):][::-1]
        return {"folder": folder, "total_matches": total, "returned": len(uids), "emails": _fetch_summaries(conn, uids)}


@mcp.tool()
def read_email(folder: str, uid: int, prefer_html: bool = False, mark_as_read: bool = True, max_chars: int = 0) -> dict:
    """Lê um e-mail completo pelo UID: cabeçalhos, corpo (texto por padrão, ou HTML) e lista de anexos.
    Use download_attachment para salvar anexos. max_chars=0 usa o limite padrão (MAX_BODY_CHARS)."""
    with imap_conn(folder, readonly=not mark_as_read) as conn:
        raw = _fetch_raw(conn, uid, peek=not mark_as_read)
    msg = message_from_bytes(raw, policy=policy.default)
    body, kind = _body_text(msg, prefer_html)
    limit = max_chars or CFG["max_body_chars"]
    truncated = len(body) > limit
    return {
        "uid": uid,
        "folder": folder,
        "message_id": str(msg["Message-ID"] or ""),
        "in_reply_to": str(msg["In-Reply-To"] or ""),
        "date": _fmt_date(msg["Date"]),
        "from": _decode_hdr(msg["From"]),
        "to": _decode_hdr(msg["To"]),
        "cc": _decode_hdr(msg["Cc"]),
        "reply_to": _decode_hdr(msg["Reply-To"]),
        "subject": _decode_hdr(msg["Subject"]),
        "body_type": kind,
        "body": body[:limit],
        "body_truncated": truncated,
        "attachments": [{"index": i, "filename": n, "content_type": t, "size": s} for i, n, t, s in _attachments(msg)],
    }


@mcp.tool()
def get_raw_email(folder: str, uid: int, max_chars: int = 50000) -> str:
    """Retorna a fonte RFC822 completa da mensagem (cabeçalhos brutos + MIME). Útil para diagnóstico."""
    with imap_conn(folder, readonly=True) as conn:
        raw = _fetch_raw(conn, uid)
    txt = raw.decode("utf-8", errors="replace")
    return txt[:max_chars] + ("\n...[truncado]" if len(txt) > max_chars else "")


@mcp.tool()
def download_attachment(folder: str, uid: int, index: int = -1, dest_dir: str = "") -> list[dict]:
    """Salva anexos de um e-mail em disco. index=-1 salva todos; caso contrário salva só o anexo indicado
    (índice conforme read_email). Retorna os caminhos gravados."""
    with imap_conn(folder, readonly=True) as conn:
        raw = _fetch_raw(conn, uid)
    msg = message_from_bytes(raw, policy=policy.default)
    base = Path(dest_dir or CFG["attach_dir"]).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, part in enumerate(msg.iter_attachments()):
        if index >= 0 and i != index:
            continue
        name = _safe_name(_decode_hdr(part.get_filename() or f"anexo-{i}"))
        path = base / f"{uid}_{name}"
        path.write_bytes(part.get_payload(decode=True) or b"")
        saved.append({"index": i, "filename": name, "path": str(path), "size": path.stat().st_size})
    if not saved:
        raise RuntimeError("Nenhum anexo encontrado com esse índice.")
    return saved


@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    html: bool = False,
    attachments: Optional[list[str]] = None,
    reply_to: str = "",
    save_to_sent: bool = True,
) -> dict:
    """Envia um e-mail via SMTP. Destinatários separados por vírgula ou ponto-e-vírgula.
    html=True trata body como HTML. attachments = lista de caminhos locais.
    Grava cópia na pasta Enviados via IMAP quando save_to_sent=True."""
    msg = _compose(to, subject, body, cc, bcc, html, attachments, reply_to=reply_to)
    _smtp_send(msg)
    sent = _append_sent(msg) if save_to_sent else None
    return {"status": "enviado", "message_id": msg["Message-ID"], "to": msg["To"], "saved_in": sent}


@mcp.tool()
def reply_email(
    folder: str,
    uid: int,
    body: str,
    reply_all: bool = False,
    html: bool = False,
    attachments: Optional[list[str]] = None,
    quote_original: bool = True,
    save_to_sent: bool = True,
) -> dict:
    """Responde um e-mail existente mantendo o encadeamento (In-Reply-To/References) e marca-o como respondido."""
    with imap_conn(folder) as conn:
        raw = _fetch_raw(conn, uid)
        orig = message_from_bytes(raw, policy=policy.default)
        me = parseaddr(_from_addr())[1].lower()

        reply_target = orig["Reply-To"] or orig["From"]
        to = _decode_hdr(reply_target)
        cc = ""
        if reply_all:
            others = _split_addrs(_decode_hdr(orig["To"])) + _split_addrs(_decode_hdr(orig["Cc"]))
            others = [a for a in others if parseaddr(a)[1].lower() not in (me, parseaddr(to)[1].lower())]
            cc = ", ".join(others)

        subj = _decode_hdr(orig["Subject"])
        if not re.match(r"^\s*re\s*:", subj, re.I):
            subj = "Re: " + subj

        refs = " ".join(x for x in (str(orig["References"] or ""), str(orig["Message-ID"] or "")) if x).strip()

        if quote_original:
            otext, _ = _body_text(orig, prefer_html=False)
            quoted = "\n".join("> " + ln for ln in otext.splitlines())
            header = f"Em {_fmt_date(orig['Date'])}, {_decode_hdr(orig['From'])} escreveu:"
            body = f"{body}\n\n{header}\n{quoted}" if not html else f"{body}<br><br><blockquote>{header}<br>{otext.replace(chr(10), '<br>')}</blockquote>"

        msg = _compose(to, subj, body, cc=cc, html=html, attachments=attachments,
                       in_reply_to=str(orig["Message-ID"] or ""), references=refs)
        _smtp_send(msg)
        try:
            conn.uid("STORE", str(uid), "+FLAGS", "(\\Answered)")
        except Exception:
            pass
    sent = _append_sent(msg) if save_to_sent else None
    return {"status": "enviado", "message_id": msg["Message-ID"], "to": msg["To"], "cc": msg["Cc"] or "", "saved_in": sent}


@mcp.tool()
def forward_email(folder: str, uid: int, to: str, body: str = "", cc: str = "", include_attachments: bool = True, save_to_sent: bool = True) -> dict:
    """Encaminha um e-mail (corpo em texto + anexos originais) para novos destinatários."""
    with imap_conn(folder, readonly=True) as conn:
        raw = _fetch_raw(conn, uid)
    orig = message_from_bytes(raw, policy=policy.default)
    subj = _decode_hdr(orig["Subject"])
    if not re.match(r"^\s*(fwd?|enc)\s*:", subj, re.I):
        subj = "Fwd: " + subj
    otext, _ = _body_text(orig, prefer_html=False)
    fwd = (
        f"{body}\n\n---------- Mensagem encaminhada ----------\n"
        f"De: {_decode_hdr(orig['From'])}\nData: {_fmt_date(orig['Date'])}\n"
        f"Assunto: {_decode_hdr(orig['Subject'])}\nPara: {_decode_hdr(orig['To'])}\n\n{otext}"
    )
    msg = _compose(to, subj, fwd, cc=cc)
    if include_attachments:
        for part in orig.iter_attachments():
            maintype, subtype = part.get_content_type().split("/", 1)
            msg.add_attachment(part.get_payload(decode=True) or b"", maintype=maintype, subtype=subtype,
                               filename=_decode_hdr(part.get_filename() or "anexo"))
    _smtp_send(msg)
    sent = _append_sent(msg) if save_to_sent else None
    return {"status": "enviado", "message_id": msg["Message-ID"], "to": msg["To"], "saved_in": sent}


@mcp.tool()
def set_flags(folder: str, uids: list[int], seen: Optional[bool] = None, flagged: Optional[bool] = None, answered: Optional[bool] = None) -> dict:
    """Marca/desmarca flags em um ou mais UIDs: seen (lido), flagged (estrela/importante), answered.
    Passe True para adicionar, False para remover, omita para não alterar."""
    changes = []
    with imap_conn(folder) as conn:
        ids = ",".join(str(u) for u in uids)
        for flag, val in (("\\Seen", seen), ("\\Flagged", flagged), ("\\Answered", answered)):
            if val is None:
                continue
            _check(conn.uid("STORE", ids, "+FLAGS" if val else "-FLAGS", f"({flag})"), "STORE")
            changes.append(("+" if val else "-") + flag)
    return {"folder": folder, "uids": uids, "changes": changes}


@mcp.tool()
def move_email(folder: str, uids: list[int], destination: str) -> dict:
    """Move e-mails para outra pasta (usa MOVE se o servidor suportar; senão COPY + delete + EXPUNGE)."""
    with imap_conn(folder) as conn:
        ids = ",".join(str(u) for u in uids)
        dest = _quote(destination)
        if "MOVE" in conn.capabilities:
            _check(conn.uid("MOVE", ids, dest), "MOVE")
        else:
            _check(conn.uid("COPY", ids, dest), "COPY")
            _check(conn.uid("STORE", ids, "+FLAGS", "(\\Deleted)"), "STORE")
            conn.expunge()
    return {"moved": uids, "from": folder, "to": destination}


@mcp.tool()
def delete_email(folder: str, uids: list[int], expunge: bool = True) -> dict:
    """Exclui e-mails (marca \\Deleted e, se expunge=True, remove definitivamente da pasta).
    Para 'mover para a lixeira' prefira move_email para a pasta Trash/Lixeira."""
    with imap_conn(folder) as conn:
        ids = ",".join(str(u) for u in uids)
        _check(conn.uid("STORE", ids, "+FLAGS", "(\\Deleted)"), "STORE")
        if expunge:
            conn.expunge()
    return {"deleted": uids, "folder": folder, "expunged": expunge}


@mcp.tool()
def create_folder(name: str) -> dict:
    """Cria uma pasta. Use o delimitador do servidor para subpastas (ex.: 'INBOX/Clientes' ou 'INBOX.Clientes')."""
    with imap_conn() as conn:
        _check(conn.create(_quote(name)), "CREATE")
        try:
            conn.subscribe(_quote(name))
        except Exception:
            pass
    return {"created": name}


@mcp.tool()
def delete_folder(name: str) -> dict:
    """Remove uma pasta e todas as mensagens nela. Irreversível."""
    with imap_conn() as conn:
        try:
            conn.unsubscribe(_quote(name))
        except Exception:
            pass
        _check(conn.delete(_quote(name)), "DELETE")
    return {"deleted": name}


@mcp.tool()
def rename_folder(name: str, new_name: str) -> dict:
    """Renomeia uma pasta."""
    with imap_conn() as conn:
        _check(conn.rename(_quote(name), _quote(new_name)), "RENAME")
    return {"renamed": name, "to": new_name}


@mcp.tool()
def account_info() -> dict:
    """Mostra a configuração ativa (sem senha), capacidades do servidor IMAP e a pasta de Enviados detectada."""
    with imap_conn() as conn:
        caps = sorted(conn.capabilities)
        sent = _find_sent_folder(conn)
    return {
        "user": CFG["user"],
        "from": _from_addr(),
        "imap": f"{CFG['imap_host']}:{CFG['imap_port']} ({'SSL' if CFG['imap_ssl'] else 'STARTTLS'})",
        "smtp": f"{CFG['smtp_host']}:{CFG['smtp_port']} ({CFG['smtp_security']})",
        "sent_folder": sent,
        "attach_dir": CFG["attach_dir"],
        "imap_capabilities": caps,
    }


def main() -> None:
    """Entry point (console script `imap-mail-mcp`)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
