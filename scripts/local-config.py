"""Generate private local settings without displaying their values."""
from pathlib import Path
import base64
import re
import secrets
import sys


def configure(root, port=None):
    root = Path(root)
    target = root / ".env"
    text = (target if target.exists() else root / ".env.example").read_text()
    defaults = {"LOCAL_DB_PASSWORD": secrets.token_hex(24), "BI_ADMIN_PASSWORD": secrets.token_urlsafe(24), "BI_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), "BI_SITE_NAME": "bi.localhost", "BI_LOCAL_PORT": "30310", "BI_COMPOSE_PROJECT": "bi-" + secrets.token_hex(6)}
    for name, default in defaults.items():
        match = re.search(rf"^{name}=(.*)$", text, re.M)
        if match and match[1].strip().strip("\"'"):
            continue
        line = f"{name}={default}"
        text = re.sub(rf"^{name}=.*$", lambda _: line, text, flags=re.M) if match else text.rstrip() + "\n" + line + "\n"
    if port is not None:
        if not str(port).isdigit() or not 1024 <= int(port) <= 65535:
            raise ValueError("Choose a valid local port.")
        text = re.sub(r"^BI_LOCAL_PORT=.*$", f"BI_LOCAL_PORT={port}", text, flags=re.M)
    target.write_text(text); target.chmod(0o600)
    runtime = root / ".runtime"; runtime.mkdir(exist_ok=True)
    credential = runtime / "local-admin-password"
    credential.write_text(re.search(r"^BI_ADMIN_PASSWORD=(.*)$", text, re.M)[1] + "\n")
    credential.chmod(0o600)
    print("Local settings are saved in .env. The Administrator password is in .runtime/local-admin-password; keep both files private.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--print-port":
        print(re.search(r"^BI_LOCAL_PORT=(\d+)$", Path(".env").read_text(), re.M)[1])
    else:
        configure(Path.cwd(), sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--port" else None)
