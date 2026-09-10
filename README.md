# imap-mail-mcp

Servidor MCP que conecta o Claude (Desktop, Code ou qualquer cliente MCP) a **uma ou várias contas de e-mail IMAP/SMTP** — Zimbra, Exchange, Dovecot/Postfix, cPanel, hospedagens, Gmail/Outlook com senha de app.

- Fala IMAP4rev1 e SMTP direto com o seu servidor; nada passa por terceiros.
- Única dependência externa: SDK `mcp`. Protocolo e MIME vêm da stdlib do Python.
- **Várias contas** ao mesmo tempo: toda ferramenta aceita `account=`; `search_all_accounts` varre todas as caixas de uma vez.
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
git clone https://github.com/julioc-barros/imap-mail-mcp && cd imap-mail-mcp
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
| `ATTACH_DIR` | temp do usuário | `%TEMP%\imap-mail-mcp` (Windows) ou `/tmp/imap-mail-mcp` (Linux/macOS) |
| `TLS_VERIFY` | `true` | `false` só para certificado self-signed |
| `MAX_BODY_CHARS` | `20000` | limite do corpo em `read_email` |
| `MAIL_ACCOUNT_NAME` | `principal` | apelido da conta principal |
| `MAIL_ACCOUNTS` | — | JSON inline com contas adicionais |
| `MAIL_ACCOUNTS_FILE` | — | caminho de um JSON com contas adicionais |

`ATTACH_DIR` aceita `~`, `$HOME`, `${HOME}`, `%USERPROFILE%` e `%TEMP%`. `MAIL_FROM` aceita `Nome <x@y>`, `x@y` ou só `Nome` (usa `MAIL_USER` como endereço).

### Várias contas

A conta principal vem das variáveis acima. Contas extras vão em `MAIL_ACCOUNTS` (JSON) ou num arquivo apontado por `MAIL_ACCOUNTS_FILE` — veja `accounts.example.json`. Campos omitidos **herdam da conta principal**, então para várias caixas no mesmo servidor basta nome, usuário e senha:

```json
{ "accounts": [
  { "name": "financeiro", "user": "financeiro@empresa.com.br", "password": "..." },
  { "name": "rh",         "user": "rh@empresa.com.br",         "password": "..." }
] }
```

Campos por conta: `name`, `user`, `password`, `from`, `imap_host`, `imap_port`, `imap_ssl`, `smtp_host`, `smtp_port`, `smtp_security`, `sent_folder`, `attach_dir`, `tls_verify`.

No Claude Desktop (.mcpb) o campo **"Arquivo de contas adicionais"** recebe esse JSON. Guarde o arquivo em local protegido — ele contém senhas.

Uso: `search_emails(account="financeiro", unseen=true)`, `send_email(account="rh", ...)`. `account` aceita o apelido ou o e-mail; vazio = conta principal.

## Ferramentas

Todas aceitam `account=""` (apelido ou e-mail; vazio = principal).

| Tool | O que faz |
|---|---|
| `list_accounts` | Contas configuradas e qual é a padrão |
| `account_info` | Config ativa, capacidades IMAP, pasta Enviados detectada |
| `search_all_accounts(...)` | Mesma busca em todas as contas, agrupada por conta |
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

1. Substitua `julioc-barros` em `pyproject.toml`, `manifest.json`, `server.json` e neste README.
2. No PyPI, crie o projeto `imap-mail-mcp` e habilite **Trusted Publishing** apontando para este repositório / workflow `release.yml`.
2. `git tag v0.1.0 && git push --tags` — o workflow publica no PyPI, gera o `.mcpb` e cria o Release.
3. (Opcional) Registro MCP: `mcp-publisher login github && mcp-publisher publish` usando o `server.json`.

## Notas de protocolo

- Busca com termo acentuado: um termo por chamada (limitação do `imaplib`, um literal por comando). Para mais, use `raw_criteria`.
- `read_email` marca como lido por padrão (`mark_as_read=false` usa `BODY.PEEK`).
- Cópia em Enviados via IMAP `APPEND`; se o SMTP já grava (Exchange), use `save_to_sent=false`.
- Gmail / Microsoft 365 exigem senha de aplicativo; OAuth2 não está coberto.

MIT © [Julio Barros](https://github.com/julioc-barros) · juliocbarros339@gmail.com
