"""Standalone OSV-Scanner v2 license gate; uses only the Python standard library."""
import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path


def enabled_licenses(policy):
    if not isinstance(policy, dict) or policy.get("schema_version") != 1 or not isinstance(policy.get("licenses"), list):
        raise ValueError("Invalid license policy")
    allowed, seen = [], set()
    for row in policy["licenses"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or type(row.get("enabled")) is not bool:
            raise ValueError("Invalid license entry")
        key = row["id"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,127}", key) or key.upper() in {"UNKNOWN", "NON-STANDARD", "UNLICENSED", "NOASSERTION", "NONE", "AND", "OR", "WITH"} or key.casefold() in seen:
            raise ValueError("Invalid or duplicate SPDX ID")
        seen.add(key.casefold())
        if row["enabled"]:
            allowed.append(key)
    return allowed


def evaluate_report(report, allowed):
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        raise ValueError("Invalid OSV-Scanner JSON")
    violations, packages, vulnerabilities = [], 0, 0
    for group in report["results"]:
        if not isinstance(group, dict) or not isinstance(group.get("packages"), list):
            raise ValueError("Invalid package group")
        for item in group["packages"]:
            if not isinstance(item, dict) or not isinstance(item.get("package"), dict):
                raise ValueError("Invalid package")
            packages += 1
            licenses = item.get("licenses") or []
            if not isinstance(licenses, list) or any(not isinstance(value, str) for value in licenses):
                raise ValueError("Invalid package licenses")
            findings = item.get("license_violations") or []
            vulns = item.get("vulnerabilities") or []
            if not isinstance(findings, list) or not isinstance(vulns, list):
                raise ValueError("Invalid scanner findings")
            vulnerabilities += len(vulns)
            # Conservatively require every reported identifier. Do not reinterpret
            # multiple licenses as a choice, or UNKNOWN as an approved license.
            denied = [value for value in licenses if value not in allowed]
            if not licenses or denied or findings:
                violations.append({"package": item["package"].get("name", "?"),
                                   "version": item["package"].get("version", ""),
                                   "licenses": licenses, "denied": denied, "unknown": not bool(licenses)})
    return {"status": "passed" if packages and not violations else "blocked",
            "packages": packages, "violations": violations, "vulnerabilities": vulnerabilities}


def scan(policy, scanner, root, timeout=300):
    allowed = enabled_licenses(policy)
    if not allowed:
        return {"status": "blocked", "message": "No licenses are enabled; OSS use is not permitted."}
    with tempfile.TemporaryDirectory(prefix="aiworkspace-osv-") as temporary:
        temporary = Path(temporary)
        config, output = temporary / "osv-scanner.toml", temporary / "scan.json"
        # A global empty configuration prevents per-directory license.ignore /
        # package overrides from silently excluding dependencies from this gate.
        config.write_text("# No exclusions or license overrides\n", encoding="utf-8")
        command = [str(scanner), "scan", "source", "--recursive", "--no-ignore", "--all-packages", "--format=json",
                   "--licenses=" + ",".join(allowed), "--config=" + str(config),
                   "--output-file=" + str(output), str(Path(root).resolve())]
        try:
            result = subprocess.run(command, cwd=root, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "error", "message": "Cannot run OSV-Scanner. Check installation, network and timeout."}
        if result.returncode not in (0, 1):
            return {"status": "error", "scanner_exit_code": result.returncode,
                    "message": "Scan did not complete. A missing scan target is not a successful license check."}
        try:
            report = evaluate_report(json.loads(output.read_text("utf-8")), allowed)
        except (OSError, UnicodeError, ValueError, TypeError):
            return {"status": "error", "message": "OSV-Scanner did not produce a valid license inventory."}
        report["scanner_exit_code"] = result.returncode
        return report


def main():
    parser = argparse.ArgumentParser(description="Enforce the AIWorkspace OSS license allowlist using OSV-Scanner v2")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--scanner", default="osv-scanner")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        report = scan(json.loads(Path(args.policy).read_text("utf-8")), args.scanner, Path(args.root).resolve())
    except (OSError, UnicodeError, ValueError, TypeError):
        report = {"status": "error", "message": "Cannot read a valid license policy."}
    serialized = json.dumps(report, ensure_ascii=True)
    if args.output:
        Path(args.output).write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
