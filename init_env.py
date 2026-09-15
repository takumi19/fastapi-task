from pathlib import Path
import secrets

path = Path(__file__).resolve().parent / '.env'
if path.exists():
    print('.env already exists; left unchanged.')
else:
    with path.open('x') as stream:
        stream.write(f'SECRET_KEY={secrets.token_urlsafe(48)}\n')
        stream.write(f'POSTGRES_PASSWORD={secrets.token_urlsafe(24)}\n')
        stream.write('PUBLIC_BASE_URL=http://127.0.0.1:8000\nREDIS_URL=redis://127.0.0.1:6379/0\n')
    path.chmod(0o600)
    print('Created .env with random secrets; do not commit it.')
