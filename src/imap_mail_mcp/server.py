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
import json
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import tempfile
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
# Configuração / contas
# ---------------------------------------------------------------------------
#
# Conta principal: variáveis IMAP_HOST, SMTP_HOST, MAIL_USER, MAIL_PASS...
# Contas adicionais: MAIL_ACCOUNTS (JSON inline) e/ou MAIL_ACCOUNTS_FILE
# (caminho para um JSON). Cada entrada é um objeto com "name" e, opcionalmente,
# imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_security, user,
# password, from, sent_folder, attach_dir, tls_verify. Campos omitidos herdam
# da conta principal (útil para várias caixas no mesmo servidor).


def _unresolved(v: Any) -> bool:
    """Placeholder que o host não substituiu (ex.: '${user_config.attach_dir}' quando o campo fica vazio)."""
    return isinstance(v, str) and v.strip().startswith("${") and v.strip().endswith("}")


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default).strip()
    return "" if _unresolved(v) else v


def _to_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return default if not s else s in ("1", "true", "yes", "on")


def _default_attach_dir() -> str:
    """Pasta temporária do usuário: %TEMP%\imap-mail-mcp no Windows, /tmp/imap-mail-mcp (ou $TMPDIR) no Linux/macOS."""
    return str(Path(tempfile.gettempdir()) / "imap-mail-mcp")


def _expand_path(p: str) -> str:
    """Expande ~, $HOME, ${HOME}, %USERPROFILE%, %TEMP% etc. Vazio = pasta temporária do usuário."""
    p = (p or "").strip()
    if not p:
        return _default_attach_dir()
    p = p.replace("%TEMP%", tempfile.gettempdir()).replace("${TEMP}", tempfile.gettempdir())
    p = p.replace("${HOME}", "~").replace("$HOME", "~").replace("%USERPROFILE%", "~")
    p = os.path.expandvars(p)
    return str(Path(p).expanduser())


MAX_BODY_CHARS = int(_env("MAX_BODY_CHARS", "20000") or 20000)
imaplib._MAXLINE = 10_000_000  # anexos grandes nas respostas FETCH


class Account:
    """Uma conta de e-mail (IMAP + SMTP)."""

    FIELDS = ("imap_host", "imap_port", "imap_ssl", "smtp_host", "smtp_port", "smtp_security",
              "user", "password", "from_", "sent_folder", "attach_dir", "tls_verify")

    def __init__(self, name: str, data: dict, base: Optional["Account"] = None) -> None:
        self.name = name

        def pick(key: str, default: Any) -> Any:
            for k in (key, key.rstrip("_")):
                if k in data and data[k] not in (None, "") and not _unresolved(data[k]):
                    return data[k]
            return getattr(base, key) if base is not None else default

        self.imap_host: str = str(pick("imap_host", ""))
        self.imap_port: int = int(pick("imap_port", 993) or 993)
        self.imap_ssl: bool = _to_bool(pick("imap_ssl", True), True)
        self.smtp_host: str = str(pick("smtp_host", ""))
        self.smtp_port: int = int(pick("smtp_port", 587) or 587)
        self.smtp_security: str = str(pick("smtp_security", "starttls")).lower()
        self.user: str = str(pick("user", ""))
        self.password: str = str(pick("password", ""))
        self.from_: str = str(pick("from_", ""))
        self.sent_folder: str = str(pick("sent_folder", ""))
        self.attach_dir: str = _expand_path(str(pick("attach_dir", "")))
        self.tls_verify: bool = _to_bool(pick("tls_verify", True), True)
        # a herança de user/password de outra conta não faz sentido
        if base is not None and "user" not in data:
            raise ValueError(f"Conta '{name}': campo 'user' é obrigatório.")

    def require_imap(self) -> None:
        missing = [k for k in ("imap_host", "user", "password") if not getattr(self, k)]
        if missing:
            raise RuntimeError(f"Conta '{self.name}': configuração ausente: {', '.join(missing)}")

    def require_smtp(self) -> None:
        missing = [k for k in ("smtp_host", "user", "password") if not getattr(self, k)]
        if missing:
            raise RuntimeError(f"Conta '{self.name}': configuração ausente: {', '.join(missing)}")

    def from_addr(self) -> str:
        """Header From válido. Aceita 'Nome <x@y>', 'x@y' ou só 'Nome' (usa MAIL_USER como endereço)."""
        raw = self.from_.strip()
        name, addr = parseaddr(raw) if raw else ("", "")
        if not addr or "@" not in addr:
            addr = self.user if "@" in self.user else ""
            name = name or raw
        if not addr:
            return self.user
        return formataddr((name, addr)) if name else addr

    def info(self) -> dict:
        return {
            "name": self.name,
            "user": self.user,
            "from": self.from_addr(),
            "imap": f"{self.imap_host}:{self.imap_port} ({'SSL' if self.imap_ssl else 'STARTTLS'})",
            "smtp": f"{self.smtp_host}:{self.smtp_port} ({self.smtp_security})",
            "attach_dir": self.attach_dir,
            "tls_verify": self.tls_verify,
        }


def _load_accounts() -> dict[str, Account]:
    primary_name = _env("MAIL_ACCOUNT_NAME") or "principal"
    primary = Account(primary_name, {
        "imap_host": _env("IMAP_HOST"), "imap_port": _env("IMAP_PORT"), "imap_ssl": _env("IMAP_SSL"),
        "smtp_host": _env("SMTP_HOST"), "smtp_port": _env("SMTP_PORT"), "smtp_security": _env("SMTP_SECURITY"),
        "user": _env("MAIL_USER"), "password": _env("MAIL_PASS"), "from": _env("MAIL_FROM"),
        "sent_folder": _env("SENT_FOLDER"), "attach_dir": _env("ATTACH_DIR"), "tls_verify": _env("TLS_VERIFY"),
    })
    accounts: dict[str, Account] = {}
    if primary.user:
        accounts[primary.name] = primary

    extra: list[dict] = []
    for src, raw in (("MAIL_ACCOUNTS", _env("MAIL_ACCOUNTS")), ("MAIL_ACCOUNTS_FILE", "")):
        if src == "MAIL_ACCOUNTS_FILE":
            path = _env("MAIL_ACCOUNTS_FILE")
            if not path:
                continue
            try:
                raw = Path(_expand_path(path)).read_text(encoding="utf-8")
            except OSError as e:
                print(f"[imap-mail] aviso: não foi possível ler {path}: {e}", file=sys.stderr)
                continue
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"[imap-mail] aviso: {src} não é JSON válido: {e}", file=sys.stderr)
            continue
        if isinstance(data, dict) and "accounts" in data:
            data = data["accounts"]
        if isinstance(data, dict):  # {"nome": {...}, ...}
            data = [dict(v, name=k) for k, v in data.items()]
        extra.extend(d for d in data if isinstance(d, dict))

    for i, d in enumerate(extra, 1):
        name = str(d.get("name") or d.get("user") or f"conta{i}")
        try:
            accounts[name] = Account(name, d, base=primary if primary.user else None)
        except ValueError as e:
            print(f"[imap-mail] aviso: {e}", file=sys.stderr)

    if not accounts:
        raise RuntimeError("Nenhuma conta configurada. Defina MAIL_USER/MAIL_PASS/IMAP_HOST ou MAIL_ACCOUNTS.")
    return accounts


ACCOUNTS: dict[str, Account] = _load_accounts()
DEFAULT_ACCOUNT: str = next(iter(ACCOUNTS))


def _acct(name: str = "") -> Account:
    """Resolve o nome (ou e-mail) de uma conta; vazio = conta padrão."""
    if not name:
        return ACCOUNTS[DEFAULT_ACCOUNT]
    if name in ACCOUNTS:
        return ACCOUNTS[name]
    low = name.lower()
    for a in ACCOUNTS.values():
        if a.name.lower() == low or a.user.lower() == low:
            return a
    raise ValueError(f"Conta '{name}' não encontrada. Disponíveis: {', '.join(ACCOUNTS)}")


def _ssl_ctx(acct: Account) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not acct.tls_verify:
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
def imap_conn(acct: Account, folder: Optional[str] = None, readonly: bool = False) -> Iterator[imaplib.IMAP4]:
    acct.require_imap()
    if acct.imap_ssl:
        conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(acct.imap_host, acct.imap_port, ssl_context=_ssl_ctx(acct))
    else:
        conn = imaplib.IMAP4(acct.imap_host, acct.imap_port)
        try:
            conn.starttls(_ssl_ctx(acct))
        except Exception:
            pass  # servidor sem STARTTLS
    try:
        conn.login(acct.user, acct.password)
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


def _find_sent_folder(acct: Account, conn: imaplib.IMAP4) -> Optional[str]:
    if acct.sent_folder:
        return acct.sent_folder
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


def _split_addrs(s: str) -> list[str]:
    return [a.strip() for a in re.split(r"[;,]", s or "") if a.strip()]


def _compose(acct: Account, to: str, subject: str, body: str, cc: str = "", bcc: str = "", html: bool = False,
             attachments: Optional[list[str]] = None, in_reply_to: str = "",
             references: str = "", reply_to: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = acct.from_addr()
    msg["To"] = ", ".join(_split_addrs(to))
    if cc:
        msg["Cc"] = ", ".join(_split_addrs(cc))
    if bcc:
        msg["Bcc"] = ", ".join(_split_addrs(bcc))
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=parseaddr(acct.from_addr())[1].split("@")[-1] or None)
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


def _smtp_send(acct: Account, msg: EmailMessage) -> None:
    acct.require_smtp()
    sec = acct.smtp_security
    if sec == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(acct.smtp_host, acct.smtp_port, context=_ssl_ctx(acct), timeout=60)
    else:
        server = smtplib.SMTP(acct.smtp_host, acct.smtp_port, timeout=60)
    with server:
        server.ehlo()
        if sec == "starttls":
            server.starttls(context=_ssl_ctx(acct))
            server.ehlo()
        if sec != "none" or acct.password:
            try:
                server.login(acct.user, acct.password)
            except smtplib.SMTPNotSupportedError:
                pass
        server.send_message(msg)


def _append_sent(acct: Account, msg: EmailMessage) -> Optional[str]:
    try:
        with imap_conn(acct) as conn:
            folder = _find_sent_folder(acct, conn)
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
        "Cliente de e-mail IMAP/SMTP com suporte a várias contas. Use list_accounts para ver as contas; "
        "todas as ferramentas aceitam o parâmetro opcional `account` (nome ou e-mail da conta; vazio = conta padrão). "
        "Fluxo típico: list_folders → search_emails (retorna UIDs) → read_email → reply_email/send_email. "
        "search_all_accounts busca em todas as caixas de uma vez. UIDs são estáveis dentro de cada pasta de cada conta."
    ),
)



@mcp.tool()
def list_accounts() -> dict:
    """Lista as contas de e-mail configuradas e qual é a padrão."""
    return {"default": DEFAULT_ACCOUNT, "accounts": [a.info() for a in ACCOUNTS.values()]}


@mcp.tool()
def account_info(account: str = "") -> dict:
    """Mostra a configuração ativa de uma conta (sem senha), capacidades do servidor IMAP e a pasta de Enviados detectada."""
    acct = _acct(account)
    with imap_conn(acct) as conn:
        caps = sorted(conn.capabilities)
        sent = _find_sent_folder(acct, conn)
    return dict(acct.info(), sent_folder=sent, imap_capabilities=caps)


@mcp.tool()
def list_folders(account: str = "", with_counts: bool = False) -> list[dict]:
    """Lista todas as pastas (mailboxes) de uma conta. Com with_counts=True inclui total e não lidos (mais lento).
    account: nome ou e-mail da conta; vazio = conta padrão."""
    acct = _acct(account)
    with imap_conn(acct) as conn:
        folders = _list_folders(conn)
        if with_counts:
            for f in folders:
                if "\\Noselect" in f["flags"]:
                    continue
                try:
                    typ, data = conn.status(_quote(f["name"]), "(MESSAGES UNSEEN)")
                    if typ == "OK" and data and data[0]:
                        st = data[0].decode(errors="replace")
                        m1, m2 = re.search(r"MESSAGES (\d+)", st), re.search(r"UNSEEN (\d+)", st)
                        f["messages"] = int(m1.group(1)) if m1 else None
                        f["unseen"] = int(m2.group(1)) if m2 else None
                except Exception:
                    pass
        for f in folders:
            f.pop("raw_name", None)
        return folders


def _do_search(acct: Account, folder: str, unseen: bool, flagged: bool, sender: str, to: str, subject: str,
               text: str, since: str, before: str, larger_than_kb: int, limit: int, raw_criteria: str) -> dict:
    with imap_conn(acct, folder, readonly=True) as conn:
        if raw_criteria:
            uids = _search(conn, raw_criteria, None)
        else:
            crit, lit = _build_search(unseen, flagged, sender, to, subject, text, since, before, larger_than_kb)
            uids = _search(conn, crit, lit)
        total = len(uids)
        uids = uids[-max(1, min(limit, 200)):][::-1]
        return {"account": acct.name, "folder": folder, "total_matches": total, "returned": len(uids),
                "emails": _fetch_summaries(conn, uids)}


@mcp.tool()
def search_emails(
    account: str = "",
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
    """Busca e-mails em uma pasta de uma conta. Retorna os mais recentes primeiro com UID, remetente, assunto, data e flags.
    account: nome ou e-mail da conta; vazio = conta padrão. Datas no formato YYYY-MM-DD.
    raw_criteria permite passar critério IMAP SEARCH direto (ex.: 'UNSEEN FROM "x"'), ignorando os demais filtros.
    Os filtros sender/to/subject/text são substring, case-insensitive."""
    return _do_search(_acct(account), folder, unseen, flagged, sender, to, subject, text, since, before,
                      larger_than_kb, limit, raw_criteria)


@mcp.tool()
def search_all_accounts(
    folder: str = "INBOX",
    unseen: bool = False,
    flagged: bool = False,
    sender: str = "",
    to: str = "",
    subject: str = "",
    text: str = "",
    since: str = "",
    before: str = "",
    limit_per_account: int = 10,
) -> dict:
    """Executa a mesma busca em TODAS as contas configuradas (mesma pasta em cada uma) e devolve os resultados agrupados por conta.
    Útil para 'o que chegou de novo em todas as caixas'. Erros de uma conta não interrompem as demais."""
    results = []
    for acct in ACCOUNTS.values():
        try:
            results.append(_do_search(acct, folder, unseen, flagged, sender, to, subject, text, since, before,
                                      0, limit_per_account, ""))
        except Exception as e:
            results.append({"account": acct.name, "folder": folder, "error": str(e)})
    return {"accounts": results}


@mcp.tool()
def read_email(folder: str, uid: int, account: str = "", prefer_html: bool = False,
               mark_as_read: bool = True, max_chars: int = 0) -> dict:
    """Lê um e-mail completo pelo UID: cabeçalhos, corpo (texto por padrão, ou HTML) e lista de anexos.
    account: nome ou e-mail da conta; vazio = conta padrão. Use download_attachment para salvar anexos.
    max_chars=0 usa o limite padrão (MAX_BODY_CHARS)."""
    acct = _acct(account)
    with imap_conn(acct, folder, readonly=not mark_as_read) as conn:
        raw = _fetch_raw(conn, uid, peek=not mark_as_read)
    msg = message_from_bytes(raw, policy=policy.default)
    body, kind = _body_text(msg, prefer_html)
    limit = max_chars or MAX_BODY_CHARS
    return {
        "account": acct.name,
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
        "body_truncated": len(body) > limit,
        "attachments": [{"index": i, "filename": n, "content_type": t, "size": s} for i, n, t, s in _attachments(msg)],
    }


@mcp.tool()
def get_raw_email(folder: str, uid: int, account: str = "", max_chars: int = 50000) -> str:
    """Retorna a fonte RFC822 completa da mensagem (cabeçalhos brutos + MIME). Útil para diagnóstico."""
    with imap_conn(_acct(account), folder, readonly=True) as conn:
        raw = _fetch_raw(conn, uid)
    txt = raw.decode("utf-8", errors="replace")
    return txt[:max_chars] + ("\n...[truncado]" if len(txt) > max_chars else "")


@mcp.tool()
def download_attachment(folder: str, uid: int, account: str = "", index: int = -1, dest_dir: str = "") -> list[dict]:
    """Salva anexos de um e-mail em disco. index=-1 salva todos; caso contrário salva só o anexo indicado
    (índice conforme read_email). dest_dir vazio usa a pasta de anexos da conta. Retorna os caminhos gravados."""
    acct = _acct(account)
    with imap_conn(acct, folder, readonly=True) as conn:
        raw = _fetch_raw(conn, uid)
    msg = message_from_bytes(raw, policy=policy.default)
    base = Path(_expand_path(dest_dir) if dest_dir else acct.attach_dir)
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
    account: str = "",
    cc: str = "",
    bcc: str = "",
    html: bool = False,
    attachments: Optional[list[str]] = None,
    reply_to: str = "",
    save_to_sent: bool = True,
) -> dict:
    """Envia um e-mail via SMTP pela conta indicada (account vazio = conta padrão).
    Destinatários separados por vírgula ou ponto-e-vírgula. html=True trata body como HTML.
    attachments = lista de caminhos locais. Grava cópia na pasta Enviados via IMAP quando save_to_sent=True."""
    acct = _acct(account)
    msg = _compose(acct, to, subject, body, cc, bcc, html, attachments, reply_to=reply_to)
    _smtp_send(acct, msg)
    sent = _append_sent(acct, msg) if save_to_sent else None
    return {"status": "enviado", "account": acct.name, "from": msg["From"], "message_id": msg["Message-ID"],
            "to": msg["To"], "saved_in": sent}


@mcp.tool()
def reply_email(
    folder: str,
    uid: int,
    body: str,
    account: str = "",
    reply_all: bool = False,
    html: bool = False,
    attachments: Optional[list[str]] = None,
    quote_original: bool = True,
    save_to_sent: bool = True,
) -> dict:
    """Responde um e-mail existente mantendo o encadeamento (In-Reply-To/References) e marca-o como respondido.
    account: nome ou e-mail da conta onde o e-mail está; vazio = conta padrão."""
    acct = _acct(account)
    with imap_conn(acct, folder) as conn:
        raw = _fetch_raw(conn, uid)
        orig = message_from_bytes(raw, policy=policy.default)
        me = parseaddr(acct.from_addr())[1].lower()

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
            body = (f"{body}\n\n{header}\n{quoted}" if not html
                    else f"{body}<br><br><blockquote>{header}<br>{otext.replace(chr(10), '<br>')}</blockquote>")

        msg = _compose(acct, to, subj, body, cc=cc, html=html, attachments=attachments,
                       in_reply_to=str(orig["Message-ID"] or ""), references=refs)
        _smtp_send(acct, msg)
        try:
            conn.uid("STORE", str(uid), "+FLAGS", "(\\Answered)")
        except Exception:
            pass
    sent = _append_sent(acct, msg) if save_to_sent else None
    return {"status": "enviado", "account": acct.name, "message_id": msg["Message-ID"], "to": msg["To"],
            "cc": msg["Cc"] or "", "saved_in": sent}


@mcp.tool()
def forward_email(folder: str, uid: int, to: str, account: str = "", body: str = "", cc: str = "",
                  include_attachments: bool = True, save_to_sent: bool = True) -> dict:
    """Encaminha um e-mail (corpo em texto + anexos originais) para novos destinatários."""
    acct = _acct(account)
    with imap_conn(acct, folder, readonly=True) as conn:
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
    msg = _compose(acct, to, subj, fwd, cc=cc)
    if include_attachments:
        for part in orig.iter_attachments():
            maintype, subtype = part.get_content_type().split("/", 1)
            msg.add_attachment(part.get_payload(decode=True) or b"", maintype=maintype, subtype=subtype,
                               filename=_decode_hdr(part.get_filename() or "anexo"))
    _smtp_send(acct, msg)
    sent = _append_sent(acct, msg) if save_to_sent else None
    return {"status": "enviado", "account": acct.name, "message_id": msg["Message-ID"], "to": msg["To"], "saved_in": sent}


@mcp.tool()
def set_flags(folder: str, uids: list[int], account: str = "", seen: Optional[bool] = None,
              flagged: Optional[bool] = None, answered: Optional[bool] = None) -> dict:
    """Marca/desmarca flags em um ou mais UIDs: seen (lido), flagged (estrela/importante), answered.
    Passe True para adicionar, False para remover, omita para não alterar."""
    acct = _acct(account)
    changes = []
    with imap_conn(acct, folder) as conn:
        ids = ",".join(str(u) for u in uids)
        for flag, val in (("\\Seen", seen), ("\\Flagged", flagged), ("\\Answered", answered)):
            if val is None:
                continue
            _check(conn.uid("STORE", ids, "+FLAGS" if val else "-FLAGS", f"({flag})"), "STORE")
            changes.append(("+" if val else "-") + flag)
    return {"account": acct.name, "folder": folder, "uids": uids, "changes": changes}


@mcp.tool()
def move_email(folder: str, uids: list[int], destination: str, account: str = "") -> dict:
    """Move e-mails para outra pasta da mesma conta (usa MOVE se o servidor suportar; senão COPY + delete + EXPUNGE)."""
    acct = _acct(account)
    with imap_conn(acct, folder) as conn:
        ids = ",".join(str(u) for u in uids)
        dest = _quote(destination)
        if "MOVE" in conn.capabilities:
            _check(conn.uid("MOVE", ids, dest), "MOVE")
        else:
            _check(conn.uid("COPY", ids, dest), "COPY")
            _check(conn.uid("STORE", ids, "+FLAGS", "(\\Deleted)"), "STORE")
            conn.expunge()
    return {"account": acct.name, "moved": uids, "from": folder, "to": destination}


@mcp.tool()
def delete_email(folder: str, uids: list[int], account: str = "", expunge: bool = True) -> dict:
    """Exclui e-mails (marca \\Deleted e, se expunge=True, remove definitivamente da pasta).
    Para 'mover para a lixeira' prefira move_email para a pasta Trash/Lixeira."""
    acct = _acct(account)
    with imap_conn(acct, folder) as conn:
        ids = ",".join(str(u) for u in uids)
        _check(conn.uid("STORE", ids, "+FLAGS", "(\\Deleted)"), "STORE")
        if expunge:
            conn.expunge()
    return {"account": acct.name, "deleted": uids, "folder": folder, "expunged": expunge}


@mcp.tool()
def create_folder(name: str, account: str = "") -> dict:
    """Cria uma pasta. Use o delimitador do servidor para subpastas (ex.: 'INBOX/Clientes' ou 'INBOX.Clientes')."""
    acct = _acct(account)
    with imap_conn(acct) as conn:
        _check(conn.create(_quote(name)), "CREATE")
        try:
            conn.subscribe(_quote(name))
        except Exception:
            pass
    return {"account": acct.name, "created": name}


@mcp.tool()
def delete_folder(name: str, account: str = "") -> dict:
    """Remove uma pasta e todas as mensagens nela. Irreversível."""
    acct = _acct(account)
    with imap_conn(acct) as conn:
        try:
            conn.unsubscribe(_quote(name))
        except Exception:
            pass
        _check(conn.delete(_quote(name)), "DELETE")
    return {"account": acct.name, "deleted": name}


@mcp.tool()
def rename_folder(name: str, new_name: str, account: str = "") -> dict:
    """Renomeia uma pasta."""
    acct = _acct(account)
    with imap_conn(acct) as conn:
        _check(conn.rename(_quote(name), _quote(new_name)), "RENAME")
    return {"account": acct.name, "renamed": name, "to": new_name}


def main() -> None:
    """Entry point (console script `imap-mail-mcp`)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
