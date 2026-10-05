"""Fail closed when a distributable build still has placeholder cloud values."""
import json
import base64
import re
from pathlib import Path


try:
    config = json.loads(Path("cloud_settings.json").read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Could not read cloud_settings.json: {exc}")

url = str(config.get("supabase_url", ""))
anon = str(config.get("supabase_anon_key", ""))
repo = str(config.get("github_repository", ""))
version = str(config.get("app_version", ""))
errors = []
if not url.startswith("https://") or "YOUR_PROJECT" in url:
    errors.append("Set supabase_url to your Supabase project URL.")
if not anon or "REPLACE_" in anon:
    errors.append("Set supabase_anon_key to the Supabase publishable/anon key.")
elif anon.startswith("sb_secret_"):
    errors.append("Do not put a Supabase secret/service_role key in the EXE config.")
elif anon.startswith("eyJ"):
    try:
        token_payload = json.loads(base64.urlsafe_b64decode(anon.split(".")[1] + "==="))
        if token_payload.get("role") != "anon":
            errors.append("supabase_anon_key must be a publishable/anon key, never service_role.")
    except Exception:
        errors.append("Could not validate the legacy JWT anon key. Use a publishable/anon key.")
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or "OWNER/REPOSITORY" in repo:
    errors.append("Set github_repository to OWNER/REPOSITORY.")
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    errors.append("Set app_version as MAJOR.MINOR.PATCH, for example 0.2.0.")
if errors:
    print("Configure cloud_settings.json before building:")
    for error in errors:
        print(f"- {error}")
    raise SystemExit(1)
print("Cloud build settings are configured.")
