"""Prepare only the local site in this compose project's private volumes."""
import json
import os
from pathlib import Path
import subprocess

ROOT = Path("/app")
BENCH = ROOT / ".runtime/bench"
SITES = BENCH / "sites"
SITE = os.environ.get("BI_SITE_NAME", "bi.localhost")
if not SITE or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in SITE):
    raise SystemExit("The local site name must contain only lowercase letters, numbers, dots and hyphens.")
for folder in [SITES, BENCH / "apps", BENCH / "logs"]:
    folder.mkdir(parents=True, exist_ok=True)
for target, source in [(BENCH / "env", ROOT / ".venv"), (BENCH / "apps/frappe", ROOT / "vendor/framework"), (BENCH / "apps/insights", ROOT)]:
    if not target.exists():
        target.symlink_to(source, target_is_directory=True)
# Realtime resolves the core handlers from a sibling app named `frappe`.
core_alias = ROOT / "vendor/frappe"
if not core_alias.exists():
    core_alias.symlink_to(ROOT / "vendor/framework", target_is_directory=True)
(SITES / "apps.txt").write_text("frappe\ninsights\n")
(SITES / "common_site_config.json").write_text(json.dumps({
    "db_host": "database", "db_port": 3306, "use_mysqlclient": 0,
    "redis_cache": "redis://cache:6379/0", "redis_queue": "redis://cache:6379/1", "redis_socketio": "redis://cache:6379/2",
    "socketio_port": 9000, "socketio_same_origin": True, "webserver_port": 8000, "webserver_host": "0.0.0.0",
    "default_site": SITE, "serve_default_site": True, "developer_mode": 0, "allow_error_traceback": 0,
    "server_script_enabled": 0, "enable_telemetry": False, "pause_scheduler": False, "disable_scheduler": False,
    "encryption_key": os.environ["BI_ENCRYPTION_KEY"],
    "chromium_path": "/usr/bin/chromium",
    "insights_preview_origin": "http://127.0.0.1:8000",
}, indent=2))
(SITES / "common_site_config.json").chmod(0o600)

def bench(*args):
    result = subprocess.run([str(ROOT / ".venv/bin/python"), "-m", "frappe.utils.bench_helper", "frappe", *args], cwd=SITES)
    if result.returncode:
        raise SystemExit("Local site setup did not finish. Check the private container logs.")

config_path = SITES / SITE / "site_config.json"
if not config_path.exists():
    bench("new-site", SITE, "--db-type", "mariadb", "--db-host", "database", "--db-port", "3306",
          "--db-root-password", os.environ["LOCAL_DB_PASSWORD"], "--admin-password", os.environ["BI_ADMIN_PASSWORD"],
          "--mariadb-user-host-login-scope", "%", "--set-default", "--install-app", "insights")
config = json.loads(config_path.read_text())
config.update(developer_mode=0, allow_error_traceback=0, host_name=f"http://localhost:{os.environ.get('BI_LOCAL_PORT', '30310')}")
config_path.write_text(json.dumps(config, indent=2)); config_path.chmod(0o600)
assets = SITES / "assets"; assets.mkdir(exist_ok=True)
for name, source in [("insights", ROOT / "insights/public"), ("frappe", ROOT / "vendor/framework/frappe/public")]:
    link = assets / name
    if not link.exists():
        link.symlink_to(source, target_is_directory=True)
built = ROOT / "review/built-assets.json"
if built.exists():
    (assets / "assets.json").write_text(built.read_text())
elif not (assets / "assets.json").exists():
    (assets / "assets.json").write_text("{}")
bench("--site", SITE, "migrate")
bench("--site", SITE, "enable-scheduler")
from insights.pramaan import PramaanClient
(BENCH / ".outside-configured").write_text("yes" if PramaanClient().configured else "no")
