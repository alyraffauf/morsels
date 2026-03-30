import json
import os
import time
from pathlib import Path

from authlib.jose import JsonWebKey
from flask import Flask


def _generate_flask_secret() -> str:
    return os.urandom(32).hex()


def _generate_client_jwk() -> str:
    key = JsonWebKey.generate_key("EC", "P-256", is_private=True)
    key_dict = json.loads(key.as_json(is_private=True))
    key_dict["kid"] = f"morsel-{int(time.time())}"
    return json.dumps(key_dict)


def load_config(app: Flask, data_dir: str = ".") -> None:
    """Load or generate config. Persists secrets to a file so they survive restarts."""
    secrets_path = Path(data_dir) / "secrets.json"

    if secrets_path.exists():
        secrets = json.loads(secrets_path.read_text())
    else:
        secrets = {
            "flask_secret_key": _generate_flask_secret(),
            "client_secret_jwk": _generate_client_jwk(),
        }
        secrets_path.write_text(json.dumps(secrets, indent=2))
        print(f"Generated new secrets at {secrets_path}")

    app.secret_key = secrets["flask_secret_key"]
    app.config["CLIENT_SECRET_JWK"] = secrets["client_secret_jwk"]
