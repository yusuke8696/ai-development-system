"""Shared OSS allowlists and the matching skill, scanner and CI files."""
import hashlib
import json
import re
from pathlib import Path

PRESETS = (
    ("MIT", "MIT License"), ("Apache-2.0", "Apache License 2.0"),
    ("BSD-2-Clause", "BSD 2-Clause"), ("BSD-3-Clause", "BSD 3-Clause"),
    ("ISC", "ISC License"), ("0BSD", "BSD Zero Clause"),
    ("Zlib", "zlib License"), ("BSL-1.0", "Boost Software License 1.0"),
)
SKILL_ID = "oss-license-policy"
ASSETS = Path(__file__).resolve().parent / "bundled" / "oss-license"
OSV_VERSION = "v2.6.0"
OSV_LINUX_SHA256 = "ca69b3d3cd08f889a49dc0a383122f71cc528b83803671df5fd874d97485b108"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_policy(value=None):
    defaults = {key: name for key, name in PRESETS}
    if value is None:
        value = {"schema_version": 1, "licenses": [
            {"id": key, "name": name, "enabled": True} for key, name in PRESETS]}
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("licenses"), list) or len(value["licenses"]) > 100:
        raise ValueError("ライセンス設定はschema_version=1、licenses配列（100件以内）で指定してください。")
    rows, seen = {}, set()
    preset_ids = {key.casefold(): key for key in defaults}
    for item in value["licenses"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or type(item.get("enabled")) is not bool:
            raise ValueError("各ライセンスにSPDX IDとON/OFFを指定してください。")
        key = item["id"].strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,127}", key) or key.upper() in {"UNKNOWN", "NON-STANDARD", "UNLICENSED", "NOASSERTION", "NONE", "AND", "OR", "WITH"}:
            raise ValueError("ライセンス名はSPDX ID（例: Apache-2.0）で指定してください。不明を許可する指定はできません。")
        key = preset_ids.get(key.casefold(), key)
        if key.casefold() in seen:
            raise ValueError("同じライセンスが重複しています: " + key)
        seen.add(key.casefold())
        name = item.get("name", defaults.get(key, key))
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name):
            raise ValueError("ライセンスの表示名は改行なしの1〜120文字にしてください。")
        rows[key] = {"id": key, "name": name.strip(), "enabled": item["enabled"], "custom": key not in defaults}
    # Missing choices stay visible but OFF; omission must never re-enable a license.
    ordered = [rows.pop(key, {"id": key, "name": name, "enabled": False, "custom": False}) for key, name in PRESETS]
    ordered.extend(rows[key] for key in sorted(rows))
    if len(ordered) > 100:
        raise ValueError("初期候補を含めてライセンスは100件以内にしてください。")
    return {"schema_version": 1, "licenses": ordered}


def policy_hash(value=None):
    return hashlib.sha256(canonical(normalize_policy(value)).encode()).hexdigest()


def allowed_ids(value=None):
    return [row["id"] for row in normalize_policy(value)["licenses"] if row["enabled"]]


def skill_body(value=None):
    return (ASSETS / "SKILL.md").read_text("utf-8") + "\n\n許可するSPDX ID: " + (", ".join(allowed_ids(value)) or "なし（OSSの導入不可）") + "\n"


def enforcement_files(project):
    project_id = project["id"]
    if not re.fullmatch(r"[a-z0-9-]{1,64}", project_id):
        raise ValueError("ライセンス設定のプロジェクトIDが不正です。")
    folder = ".ai-dev/oss/" + project_id
    short = project_id[:8]
    workflow = f"""name: AIWorkspace OSS {short}
on:
  pull_request:
  push:
    branches: [{json.dumps(project['base_branch'])}]
  workflow_dispatch:
permissions:
  contents: read
jobs:
  licenses:
    name: OSS license allowlist ({short})
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - name: Install pinned OSV-Scanner
        shell: bash
        run: |
          curl --fail --location --retry 3 --output "$RUNNER_TEMP/osv-scanner" https://github.com/google/osv-scanner/releases/download/{OSV_VERSION}/osv-scanner_linux_amd64
          echo "{OSV_LINUX_SHA256}  $RUNNER_TEMP/osv-scanner" | sha256sum --check -
          chmod +x "$RUNNER_TEMP/osv-scanner"
      - name: Enforce allowed OSS licenses
        shell: bash
        run: python3 {folder}/check_licenses.py --policy {folder}/policy.json --scanner "$RUNNER_TEMP/osv-scanner" --root . --output "$RUNNER_TEMP/aiworkspace-licenses-{short}.json"
"""
    return {
        folder + "/policy.json": canonical(normalize_policy(project.get("license_policy"))) + "\n",
        folder + "/skills/oss-license-policy/SKILL.md": skill_body(project.get("license_policy")),
        folder + "/check_licenses.py": (ASSETS / "check_licenses.py").read_text("utf-8"),
        ".github/workflows/aiworkspace-oss-" + project_id + ".yml": workflow,
    }


def enforcement_hashes(files):
    return {path: hashlib.sha256(body.encode()).hexdigest() for path, body in files.items()}
