"""Heuristic redaction for text sent to remote model and retrieval services."""
from __future__ import annotations

import re


_CREDENTIAL = re.compile(
    r'''(?ix)
    (\b(?:api[_-]?key|password|authorization|secret|access[_-]?token|refresh[_-]?token|client[_-]?secret)\b["']?\s*[=:]\s*)
    ("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|Bearer\s+[\w.-]+|\[REDACTED\]|[^\s,;}"'\]]+)
    '''
)
_BEARER = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')
_PRIVATE_KEY = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', re.S)
_COMMON_TOKEN = re.compile(r'(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})(?![A-Za-z0-9])')
_ABSOLUTE_PATH_KEYS = {'repo_path', 'workspace', 'source_root', 'storage_path', 'absolute_path'}
_SECRET_KEY = re.compile(r'(?i)(?:^|[_-])(?:api[_-]?key|password|secret|token|authorization|cookie|credential)(?:$|[_-])')


def redact_text(text: str) -> str:
    """Redact recognizable credential patterns; this is not a PII detector."""
    def replace(match: re.Match[str]) -> str:
        value = match[2]
        quote = value[0] if value[0] in ('"', "'") else ''
        return f'{match[1]}{quote}[REDACTED]{quote}'

    text = _PRIVATE_KEY.sub('[REDACTED PRIVATE KEY]', text)
    text = _COMMON_TOKEN.sub('[REDACTED]', text)
    return _BEARER.sub('Bearer [REDACTED]', _CREDENTIAL.sub(replace, text))


def redact_value(value, key: str | None = None):
    """Recursively redact secret-bearing fields and text in a JSON-like value."""
    if key and _SECRET_KEY.search(key):
        return '[REDACTED]'
    if key and key.lower() in _ABSOLUTE_PATH_KEYS:
        return '[LOCAL_PATH_OMITTED]'
    if isinstance(value, dict):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item, key) for item in value]
    if isinstance(value, str):
        # Absolute paths are often usernames or host layout; preserve relative
        # paths while hiding values explicitly carried as path fields.
        if key and (key.lower() in {'path', 'file_path'} or key.lower().endswith('_paths')):
            if value.startswith('/') or (len(value) > 2 and value[1] == ':' and value[2] in ('/', '\\')):
                return '[LOCAL_PATH_OMITTED]'
        return redact_text(value)
    return value
