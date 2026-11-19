"""Create private local-only bootstrap credentials; never prints their values."""
import os
from pathlib import Path
import secrets

path = Path(__file__).resolve().parents[1] / 'infra/langfuse/.env'
values = {name: secrets.token_hex(32) for name in ['POSTGRES_PASSWORD', 'REDIS_AUTH', 'CLICKHOUSE_PASSWORD', 'MINIO_ROOT_PASSWORD', 'SALT', 'ENCRYPTION_KEY', 'NEXTAUTH_SECRET', 'LANGFUSE_INIT_USER_PASSWORD']}
values.update({
    'LANGFUSE_INIT_ORG_ID': 'role-runtime-local',
    'LANGFUSE_INIT_ORG_NAME': 'Role Runtime Local',
    'LANGFUSE_INIT_PROJECT_ID': 'orchestrator-observability',
    'LANGFUSE_INIT_PROJECT_NAME': 'Orchestrator Synthetic Validation',
    'LANGFUSE_INIT_PROJECT_PUBLIC_KEY': 'pk-lf-' + secrets.token_hex(16),
    'LANGFUSE_INIT_PROJECT_SECRET_KEY': 'sk-lf-' + secrets.token_hex(32),
    'LANGFUSE_INIT_USER_EMAIL': 'local@role-runtime.test',
    'LANGFUSE_INIT_USER_NAME': 'Local Developer',
})
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write(''.join(f'{k}={v}\n' for k,v in values.items()))
print(f'Created {path}; values omitted. Existing files are never overwritten.')
