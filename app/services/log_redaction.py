"""
Nessuna credenziale nei log.

Un URL loggato (ad esempio la riga ``HTTP Request: GET ...`` di httpx, o il
testo di un ``HTTPStatusError``) può contenere credenziali nella query
string — un token e il suo salt, una API key — o nelle userinfo
(``https://user:pass@host``), e chi legge i log potrebbe riusarle.

:func:`redact` sostituisce il valore di ogni parametro sensibile con
``***``; :func:`install_log_redaction` la applica a ogni record di log del
processo (API e worker), qualunque logger lo emetta e qualunque handler lo
scriva, tramite la record factory di ``logging``.
"""
from __future__ import annotations

import logging
import re

REDACTED = "***"

# Nomi di parametro che portano credenziali. Quelli di una lettera sono le
# convenzioni di alcune API (utente, token, salt, password): valgono solo
# come parametri di una query string, mai come testo libero.
SENSITIVE_PARAMS = (
    "u", "t", "s", "p",
    "token", "access_token", "refresh_token", "id_token", "auth", "authorization",
    "api_key", "apikey", "key", "client_secret", "secret", "password", "passwd", "pass",
    "signature", "sig", "code", "session", "sessionid", "jwt",
)

_QUERY_PARAM = re.compile(
    r"(?P<lead>[?&;](?:%s)=)(?P<value>[^&;#\s'\"<>]*)" % "|".join(map(re.escape, SENSITIVE_PARAMS)),
    re.IGNORECASE,
)
_USERINFO = re.compile(r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/@\s'\"]+)@", re.IGNORECASE)


def redact(text: str) -> str:
    if not text or ("=" not in text and "@" not in text):
        return text
    text = _QUERY_PARAM.sub(lambda m: m.group("lead") + REDACTED, text)
    return _USERINFO.sub(lambda m: m.group("scheme") + REDACTED + "@", text)


def _redact_arg(value):
    """Un argomento del messaggio ripulito, senza cambiarne il tipo quando
    non contiene nulla da togliere (``%d`` resta un intero, un formatter che
    spacchetta gli argomenti li ritrova tutti)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    cleaned = redact(text)
    return value if cleaned == text else cleaned


_installed = False


def install_log_redaction() -> None:
    """Idempotente. Ripulisce il messaggio e i suoi argomenti alla creazione
    del record (solo per i record che superano il livello del logger), così
    nessun handler aggiunto dopo, nemmeno da un framework, li vede in
    chiaro. Lo stesso vale per il testo dell'eccezione allegata."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()
    formatter = logging.Formatter()

    def factory(*args, **kwargs) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_redact_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _redact_arg(v) for k, v in record.args.items()}
            if record.exc_info and not record.exc_text:
                record.exc_text = redact(formatter.formatException(record.exc_info))
        except Exception:  # noqa: BLE001 — un record malformato resta com'è
            pass
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
