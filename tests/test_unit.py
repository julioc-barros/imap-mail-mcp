import os

os.environ.setdefault("IMAP_HOST", "x")
os.environ.setdefault("SMTP_HOST", "x")
os.environ.setdefault("MAIL_USER", "u@x.com")
os.environ.setdefault("MAIL_PASS", "p")

from imap_mail_mcp import server as s  # noqa: E402


def test_utf7_roundtrip():
    for name in ["INBOX", "Itens Enviados", "Ação & Reação", "日本語", "A&B"]:
        assert s.imap_utf7_decode(s.imap_utf7_encode(name)) == name


def test_utf7_known_value():
    assert s.imap_utf7_encode("Ação") == "A&AOcA4w-o"


def test_quote_folder():
    assert s._quote('Pasta "x"') == '"Pasta \\"x\\""'


def test_parse_list():
    p = s._parse_list(b'(\\HasNoChildren \\Sent) "/" "Sent Items"')
    assert p["name"] == "Sent Items" and "\\Sent" in p["flags"] and p["delimiter"] == "/"
    p = s._parse_list(b'(\\HasNoChildren) "." INBOX')
    assert p["name"] == "INBOX" and p["delimiter"] == "."


def test_build_search_ascii():
    crit, lit = s._build_search(True, False, "joao", "", "report", "", "2026-09-01", "", 0)
    assert crit == 'UNSEEN SINCE 01-Sep-2026 FROM "joao" SUBJECT "report"' and lit is None


def test_build_search_literal_last():
    crit, lit = s._build_search(False, False, "", "", "Relatório", "x", "", "", 0)
    assert crit.endswith("SUBJECT") and lit == "Relatório".encode()


def test_compose_html_with_reply_headers():
    m = s._compose(s._acct(), "a@b.com; c@d.com", "T", "<b>oi</b>", html=True, in_reply_to="<x@y>", references="<w@y> <x@y>")
    assert m["To"] == "a@b.com, c@d.com"
    assert m["In-Reply-To"] == "<x@y>" and m.get_content_type() == "multipart/alternative"


def test_from_addr_fallbacks():
    a = s.Account("x", {"user": "julio@kropneus.com", "from": "Julio Barros"})
    assert a.from_addr() == "Julio Barros <julio@kropneus.com>"
    a = s.Account("x", {"user": "julio@kropneus.com", "from": ""})
    assert a.from_addr() == "julio@kropneus.com"
    a = s.Account("x", {"user": "julio", "from": "Julio <j@k.com>"})
    assert a.from_addr() == "Julio <j@k.com>"


def test_expand_path(monkeypatch):
    home = str(s.Path.home())
    assert s._expand_path("${HOME}/anexos") == str(s.Path(home) / "anexos")
    assert s._expand_path("~/anexos") == str(s.Path(home) / "anexos")
    assert s._expand_path("%USERPROFILE%/anexos") == str(s.Path(home) / "anexos")
    import tempfile
    assert s._expand_path("") == str(s.Path(tempfile.gettempdir()) / "imap-mail-mcp")
    assert s._expand_path("%TEMP%/x") == str(s.Path(tempfile.gettempdir()) / "x")


def test_extra_accounts_inherit(monkeypatch):
    monkeypatch.setenv("MAIL_ACCOUNTS", '[{"name":"financeiro","user":"fin@x.com","password":"p2"},'
                                        '{"name":"outro","user":"o@y.com","password":"p3","imap_host":"imap.y.com"}]')
    accts = s._load_accounts()
    assert list(accts) == ["principal", "financeiro", "outro"]
    assert accts["financeiro"].imap_host == "x" and accts["financeiro"].password == "p2"
    assert accts["outro"].imap_host == "imap.y.com" and accts["outro"].smtp_host == "x"


def test_acct_lookup_by_email(monkeypatch):
    monkeypatch.setattr(s, "ACCOUNTS", {"principal": s.Account("principal", {"user": "u@x.com", "password": "p"})})
    monkeypatch.setattr(s, "DEFAULT_ACCOUNT", "principal")
    assert s._acct("U@X.COM").name == "principal"
    assert s._acct("").name == "principal"
    import pytest
    with pytest.raises(ValueError):
        s._acct("nada")


def test_unresolved_placeholder_treated_as_empty(monkeypatch):
    monkeypatch.setenv("ATTACH_DIR", "${user_config.attach_dir}")
    monkeypatch.setenv("MAIL_FROM", "${user_config.mail_from}")
    a = s._load_accounts()["principal"]
    import tempfile
    assert a.attach_dir == str(s.Path(tempfile.gettempdir()) / "imap-mail-mcp")
    assert a.from_addr() == "u@x.com"
