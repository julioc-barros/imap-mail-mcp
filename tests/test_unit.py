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
    m = s._compose("a@b.com; c@d.com", "T", "<b>oi</b>", html=True, in_reply_to="<x@y>", references="<w@y> <x@y>")
    assert m["To"] == "a@b.com, c@d.com"
    assert m["In-Reply-To"] == "<x@y>" and m.get_content_type() == "multipart/alternative"
