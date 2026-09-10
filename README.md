# imap-mail-mcp

Servidor MCP que conecta o Claude (Desktop, Code ou qualquer cliente MCP) a **qualquer conta de e-mail IMAP/SMTP** — Zimbra, Exchange, Dovecot/Postfix, cPanel, hospedagens, Gmail/Outlook com senha de app.

- Fala IMAP4rev1 e SMTP direto com o seu servidor; nada passa por terceiros.
- Única dependência externa: SDK `mcp`. Protocolo e MIME vêm da stdlib do Python.
- Todas as operações por **UID**; pastas com acento em UTF-7; busca não-ASCII com `CHARSET UTF-8`.

## Instalação

### Claude Desktop — um clique (.mcpb)
Baixe o `imap-mail-mcp-X.Y.Z.mcpb` em **Releases**, abra Claude Desktop → Configurações → Extensões e arraste o arquivo. O Claude pede host, usuário e senha num formulário (a senha vai para o keychain do sistema).

### Claude Code / qualquer cliente MCP (PyPI)
```bash
claude mcp add imap-mail \
  -e IMAP_HOST=mail.empresa.com.br -e SMTP_HOST=mail.empresa.com.br \
  -e MAIL_USER=voce@empresa.com.br -e MAIL_PASS='***' \
  -- uvx imap-mail-mcp
```
Ou no `claude_desktop_config.json` / `.mcp.json`:
```json
{
  "mcpServers": {
    "imap-mail": {
      "command": "uvx",
      "args": ["imap-mail-mcp"],
      "env": { "IMAP_HOST": "...", "SMTP_HOST": "...", "MAIL_USER": "...", "MAIL_PASS": "..." }
    }
  }
}
```

### A partir do código
```bash
git clone https://github.com/SEU-USUARIO/imap-mail-mcp && cd imap-mail-mcp
uv sync && uv run imap-mail-mcp        # ou: pip install -e . && imap-mail-mcp
```

## Configuração (variáveis de ambiente)

| Variável | Padrão | Descrição |
|---|---|---|
| `IMAP_HOST` | — | obrigatório |
| `IMAP_PORT` | `993` | 993 SSL ou 143 STARTTLS |
| `IMAP_SSL` | `true` | `false` = STARTTLS na 143 |
| `SMTP_HOST` | — | obrigatório |
| `SMTP_PORT` | `587` | 587 / 465 / 25 |
| `SMTP_SECURITY` | `starttls` | `starttls` \| `ssl` \| `none` |
| `MAIL_USER` / `MAIL_PASS` | — | obrigatórios |
| `MAIL_FROM` | `MAIL_USER` | `Nome <voce@empresa.com.br>` |
| `SENT_FOLDER` | auto | detecta `\Sent`, "Sent", "Itens Enviados"... |
| `ATTACH_DIR` | `~/mcp-mail-attachments` | destino dos anexos |
| `TLS_VERIFY` | `true` | `false` só para certificado self-signed |
| `MAX_BODY_CHARS` | `20000` | limite do corpo em `read_email` |

## Ferramentas

| Tool | O que faz |
|---|---|
| `account_info` | Config ativa, capacidades IMAP, pasta Enviados detectada |
| `list_folders(with_counts)` | Lista pastas, opcionalmente com total/não lidos |
| `search_emails(...)` | `unseen`, `flagged`, `sender`, `to`, `subject`, `text`, `since`, `before`, `larger_than_kb`, `limit` ou `raw_criteria` (IMAP SEARCH direto) |
| `read_email(folder, uid)` | Cabeçalhos + corpo (texto ou HTML) + lista de anexos |
| `get_raw_email` | Fonte RFC822 completa |
| `download_attachment` | Salva um ou todos os anexos |
| `send_email` | To/Cc/Bcc, texto ou HTML, anexos, Reply-To; cópia em Enviados |
| `reply_email` | Responde (ou a todos) com In-Reply-To/References; marca `\Answered` |
| `forward_email` | Encaminha com anexos originais |
| `set_flags` | `\Seen`, `\Flagged`, `\Answered` em lote |
| `move_email` | MOVE, ou COPY+DELETE+EXPUNGE se o servidor não suportar |
| `delete_email` | `\Deleted` + EXPUNGE |
| `create_folder` / `rename_folder` / `delete_folder` | Pastas |

## Desenvolvimento

```bash
uv sync --group dev
uv run pytest -q
npx -y @anthropic-ai/mcpb validate manifest.json
npx -y @anthropic-ai/mcpb pack . dist/imap-mail-mcp.mcpb   # bundle local
```

Teste de integração ponta a ponta pode ser feito contra o [GreenMail](https://greenmail-mail-test.github.io/greenmail/) standalone (`-Dgreenmail.setup.test.all`), portas 3143/3025.

## Publicação

1. Substitua `SEU-USUARIO` em `pyproject.toml`, `manifest.json`, `server.json` e neste README.
2. No PyPI, crie o projeto `imap-mail-mcp` e habilite **Trusted Publishing** apontando para este repositório / workflow `release.yml`.
3. `git tag v0.1.0 && git push --tags` — o workflow publica no PyPI, gera o `.mcpb` e cria o Release.
4. (Opcional) Registro MCP: `mcp-publisher login github && mcp-publisher publish` usando o `server.json`.

## Notas de protocolo

- Busca com termo acentuado: um termo por chamada (limitação do `imaplib`, um literal por comando). Para mais, use `raw_criteria`.
- `read_email` marca como lido por padrão (`mark_as_read=false` usa `BODY.PEEK`).
- Cópia em Enviados via IMAP `APPEND`; se o SMTP já grava (Exchange), use `save_to_sent=false`.
- Gmail / Microsoft 365 exigem senha de aplicativo; OAuth2 não está coberto.

MIT © Julio
