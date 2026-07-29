#!/usr/bin/env python3

import argparse
import datetime
import glob
import json
import re
import urllib.error
import urllib.request
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path


HOME = Path.home()
REPORT_PATH = HOME / "NODE-DOCTOR.txt"

PASS_COUNT = 0
WARN_COUNT = 0
FAIL_COUNT = 0
RESULTS = []
COMPONENT_SCORES = {}

APP_CATALOG = {
    "bitcoin": {
        "name": "Bitcoin Core",
        "repo": "bitcoin/bitcoin",
        "commands": ["bitcoind --version"],
        "service": "bitcoind",
        "release_policy": "github_stable",
    },
    "lnd": {
        "name": "LND",
        "repo": "lightningnetwork/lnd",
        "commands": ["lnd --version", "lncli version"],
        "service": "lnd",
        # LND's normal production releases contain "-beta" in the tag.
        # GitHub's prerelease flag, not the word "beta", determines channel.
        "release_policy": "github_flag",
    },
    "lndg": {
        "name": "LNDg",
        "repo": "cryptosharks131/lndg",
        "docker_match": "lndg",
        "docker_version_commands": [
            "git -C /app describe --tags --always 2>/dev/null",
            "git -C /lndg describe --tags --always 2>/dev/null",
        ],
        "release_policy": "github_flag",
    },
    "thunderhub": {
        "name": "ThunderHub",
        "repo": "apotdevin/thunderhub",
        "paths": [HOME / "thunderhub", HOME / "ThunderHub"],
        "service": "thunderhub",
        "release_policy": "github_flag",
    },
    "rtl": {
        "name": "RTL",
        "repo": "Ride-The-Lightning/RTL",
        "paths": [HOME / "RTL", HOME / "rtl"],
        "service": "RTL",
        "package_json": True,
        "release_policy": "github_flag",
    },
    "lnbits": {
        "name": "LNbits",
        "repo": "lnbits/lnbits",
        "paths": [HOME / "lnbits", HOME / "LNbits"],
        "service": "lnbits",
        "release_policy": "github_flag",
    },
    "fulcrum": {
        "name": "Fulcrum",
        "repo": "cculianu/Fulcrum",
        "commands": ["Fulcrum --version", "fulcrum --version"],
        "service": "fulcrum",
        "release_policy": "github_flag",
    },
    "mempool": {
        "name": "Mempool",
        "repo": "mempool/mempool",
        "docker_match": "mempool/backend",
        "docker_digest_check": True,
        "release_policy": "github_flag",
    },
    "pihole": {
        "name": "Pi-hole",
        "repo": "pi-hole/pi-hole",
        "commands": ["pihole -v"],
        "service": "pihole-FTL",
        "release_policy": "github_flag",
    },
}
UPDATE_CACHE = {}


class Colors:
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[34m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


USE_COLOR = sys.stdout.isatty()


def color(text, code):
    if not USE_COLOR:
        return text
    return f"{code}{text}{Colors.RESET}"


def run(command, timeout=20):
    try:
        result = subprocess.run(
            command,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out"
    except Exception as exc:
        return 1, "", str(exc)


def command_exists(command):
    return shutil.which(command) is not None


def add_result(level, message):
    global PASS_COUNT, WARN_COUNT, FAIL_COUNT

    if level == "PASS":
        PASS_COUNT += 1
        prefix = color("[PASS]", Colors.GREEN)
    elif level == "WARN":
        WARN_COUNT += 1
        prefix = color("[WARN]", Colors.YELLOW)
    elif level == "FAIL":
        FAIL_COUNT += 1
        prefix = color("[FAIL]", Colors.RED)
    else:
        prefix = color("[INFO]", Colors.BLUE)

    line = f"{prefix} {message}"
    print(line)
    RESULTS.append(f"[{level}] {message}")


def passed(message):
    add_result("PASS", message)


def warned(message):
    add_result("WARN", message)


def failed(message):
    add_result("FAIL", message)


def info(message):
    add_result("INFO", message)


def section(title):
    print()
    print(color("=" * 60, Colors.BOLD))
    print(color(title, Colors.BOLD))
    print(color("=" * 60, Colors.BOLD))


def parse_json_command(command, timeout=20):
    code, stdout, stderr = run(command, timeout=timeout)

    if code != 0 or not stdout:
        return None, stderr or stdout

    try:
        return json.loads(stdout), ""
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON: {exc}"


def human_age(seconds):
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def parse_iso_timestamp(value):
    if not value:
        return None

    value = value.strip()

    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    # Docker commonly returns nine fractional digits, while Python's
    # fromisoformat expects microseconds. Trim only the fractional portion.
    value = re.sub(
        r"(\.\d{6})\d+(?=[+-]\d{2}:\d{2}$)",
        r"\1",
        value,
    )

    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None



def normalize_version(value):
    if not value:
        return None

    match = re.search(
        r"v?(\d+(?:\.\d+){1,3}(?:[-+._]?[0-9A-Za-z.-]+)?)",
        value,
    )
    return match.group(1).replace("_", "-") if match else None


def parsed_version(value):
    normalized = normalize_version(value)
    if not normalized:
        return None

    match = re.match(
        r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:[-+.]?(.*))?$",
        normalized,
    )
    if not match:
        return None

    numbers = tuple(int(part or 0) for part in match.groups()[:4])
    suffix = (match.group(5) or "").lower()

    # Stable sorts above rc, beta, alpha and development snapshots.
    if not suffix:
        stage = 4
        stage_number = 0
    elif re.search(r"(?:^|[.-])rc(?:[.-]?(\d+))?", suffix):
        stage = 3
        found = re.search(r"(?:^|[.-])rc(?:[.-]?(\d+))?", suffix)
        stage_number = int(found.group(1) or 0)
    elif "beta" in suffix:
        stage = 2
        found = re.search(r"beta(?:[.-]?(\d+))?", suffix)
        stage_number = int(found.group(1) or 0) if found else 0
    elif "alpha" in suffix:
        stage = 1
        found = re.search(r"alpha(?:[.-]?(\d+))?", suffix)
        stage_number = int(found.group(1) or 0) if found else 0
    else:
        stage = 0
        found = re.search(r"(\d+)", suffix)
        stage_number = int(found.group(1)) if found else 0

    return numbers + (stage, stage_number)


def version_key(value):
    return parsed_version(value) or (0, 0, 0, 0, 0, 0)


def github_releases(repo):
    if repo in UPDATE_CACHE:
        return UPDATE_CACHE[repo]

    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases?per_page=100",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "node-doctor/2.4.1",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw_releases = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        result = {"stable": None, "beta": None, "all": [], "error": str(exc)}
        UPDATE_CACHE[repo] = result
        return result

    releases = []
    for release in raw_releases:
        if release.get("draft") or not release.get("tag_name"):
            continue

        version = normalize_version(release["tag_name"])
        if not version:
            continue

        releases.append({
            "tag": release["tag_name"],
            "version": version,
            "beta": bool(release.get("prerelease")),
            "published": release.get("published_at"),
            "body": release.get("body") or "",
        })

    # Never trust GitHub API ordering. Sort semantically.
    releases.sort(key=lambda item: version_key(item["version"]), reverse=True)
    stable_items = [item for item in releases if not item["beta"]]
    beta_items = [item for item in releases if item["beta"]]

    result = {
        "stable": stable_items[0] if stable_items else None,
        "beta": beta_items[0] if beta_items else None,
        "all": releases,
        "error": "",
    }
    UPDATE_CACHE[repo] = result
    return result


def docker_container(match):
    if not command_exists("docker"):
        return None

    code, output, _ = run(
        "docker ps --format '{{.Names}}|{{.Image}}'",
        timeout=15,
    )
    if code != 0:
        return None

    match = match.lower()
    for line in output.splitlines():
        if "|" not in line:
            continue
        name, image = line.split("|", 1)
        if match in name.lower() or match in image.lower():
            return {"name": name, "image": image}
    return None


def docker_compose_dir(container_name):
    template = '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
    code, output, _ = run(
        f"docker inspect --format='{template}' '{container_name}'",
        timeout=15,
    )
    if code == 0 and output and output != "<no value>":
        return output.strip()
    return None


RUNTIME_LAUNCHERS = {
    "node", "npm", "npx", "yarn", "pnpm",
    "python", "python3", "uv", "uvicorn",
    "bash", "sh", "env",
}


def systemd_service_info(service):
    """Return service executable and working directory without confusing a
    generic runtime (node/npm/python/uv) with the application it launches.
    """
    if not service:
        return None

    code, output, _ = run(
        (
            f"systemctl show '{service}' "
            "--property=ExecStart --property=WorkingDirectory --value"
        ),
        timeout=10,
    )
    if code != 0 or not output:
        return None

    lines = output.splitlines()
    exec_start = lines[0].strip() if lines else ""
    working_dir = lines[1].strip() if len(lines) > 1 else ""

    candidates = re.findall(r"(?:path=)?(/[^ ;}]+)", exec_start)
    executable = None
    runtime = None

    for candidate in candidates:
        path = Path(candidate)
        if not (path.exists() and path.is_file()):
            continue

        if path.name.lower() in RUNTIME_LAUNCHERS:
            if runtime is None:
                runtime = path
            continue

        executable = path
        break

    if executable is None:
        executable = runtime

    work_path = None
    if working_dir and working_dir not in ("-", "n/a"):
        candidate = Path(working_dir)
        if candidate.exists() and candidate.is_dir():
            work_path = candidate

    return {
        "executable": executable,
        "working_directory": work_path,
        "exec_start": exec_start,
        "is_runtime": bool(
            executable and executable.name.lower() in RUNTIME_LAUNCHERS
        ),
    }


def systemd_exec_path(service):
    info = systemd_service_info(service)
    return info["executable"] if info else None


def version_from_package_json(path):
    package_file = Path(path) / "package.json"
    if not package_file.exists():
        return None

    try:
        data = json.loads(package_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    return normalize_version(str(data.get("version", "")))


def version_from_git(path):
    code, output, _ = run(
        f"git -C '{path}' describe --tags --always 2>/dev/null",
        timeout=10,
    )
    return normalize_version(output) if code == 0 else None


def docker_exec_version(container, commands):
    for command in commands:
        code, output, _ = run(
            f"docker exec '{container}' sh -c {json.dumps(command)}",
            timeout=15,
        )
        version = normalize_version(output)
        if code == 0 and version:
            return version
    return None


def local_docker_digest(image):
    code, output, _ = run(
        f"docker image inspect '{image}' --format='{{{{json .RepoDigests}}}}'",
        timeout=20,
    )
    if code != 0 or not output or output == "null":
        return None

    try:
        digests = json.loads(output)
    except json.JSONDecodeError:
        return None

    for digest in digests:
        if "@" in digest:
            return digest.split("@", 1)[1]
    return None


def remote_docker_digest(image):
    # docker manifest inspect contacts the registry but does not pull/install.
    code, output, _ = run(
        f"docker manifest inspect '{image}'",
        timeout=45,
    )
    if code != 0 or not output:
        return None

    try:
        manifest = json.loads(output)
    except json.JSONDecodeError:
        return None

    descriptor = manifest.get("Descriptor") or manifest.get("descriptor") or {}
    digest = descriptor.get("digest")
    if digest:
        return digest

    # Single-platform schema manifests may expose config.digest.
    config = manifest.get("config") or {}
    return config.get("digest")


def installed_app(app_key):
    app = APP_CATALOG[app_key]

    for command in app.get("commands", []):
        executable = command.split()[0]
        if not command_exists(executable):
            continue

        code, stdout, stderr = run(command, timeout=15)
        version = normalize_version(stdout + "\n" + stderr)
        if version:
            return {
                "version": version,
                "type": "binary",
                "source": command,
            }

    for path in app.get("paths", []):
        if not path.exists():
            continue

        version = None
        if app.get("package_json"):
            version = version_from_package_json(path)
        if not version and (path / ".git").exists():
            version = version_from_git(path)

        if version:
            return {
                "version": version,
                "type": "git",
                "source": str(path),
                "path": str(path),
            }

    match = app.get("docker_match")
    if match:
        container = docker_container(match)
        if container:
            version = docker_exec_version(
                container["name"],
                app.get("docker_version_commands", []),
            )
            result = {
                "version": version,
                "type": "docker-compose",
                "source": container["image"],
                "container": container["name"],
                "image": container["image"],
            }

            if app.get("docker_digest_check"):
                result["local_digest"] = local_docker_digest(container["image"])
                result["remote_digest"] = remote_docker_digest(container["image"])

            return result

    # Last-resort service detection. Generic launchers such as npm, node,
    # python, and uv are not the application and must never supply its version.
    service_info = systemd_service_info(app.get("service"))
    if service_info:
        executable = service_info.get("executable")
        working_dir = service_info.get("working_directory")
        is_runtime = service_info.get("is_runtime", False)

        # For runtime-launched services, inspect the application's working
        # directory instead of running `npm --version`, `node --version`, etc.
        if is_runtime:
            version = None
            if working_dir:
                version = version_from_package_json(working_dir)
                if not version and (working_dir / ".git").exists():
                    version = version_from_git(working_dir)

            source = f"systemd runtime: {executable}"
            if working_dir:
                source += f"; app dir: {working_dir}"

            return {
                "version": version,
                "type": "git" if working_dir else "service",
                "source": source,
                "path": str(working_dir or executable),
            }

        if executable:
            commands = [
                f"'{executable}' --version",
                f"'{executable}' -version",
                f"'{executable}' -v",
            ]
            for command in commands:
                code, stdout, stderr = run(command, timeout=15)
                version = normalize_version(stdout + "\n" + stderr)
                if version:
                    return {
                        "version": version,
                        "type": "binary",
                        "source": f"systemd: {executable}",
                        "path": str(executable),
                    }

            return {
                "version": None,
                "type": "binary",
                "source": f"systemd: {executable}",
                "path": str(executable),
            }

    return None


def comparable_version(app_key, value):
    """Return a project-aware version used only for update comparisons."""
    normalized = normalize_version(value)
    if not normalized:
        return None

    # RTL publishes package versions such as 0.15.9-beta while the matching
    # stable GitHub release is tagged v0.15.9. Treat those as the same build.
    if app_key == "rtl":
        normalized = re.sub(r"-beta$", "", normalized, flags=re.IGNORECASE)

    return normalized


def update_severity(installed_version, target_version):
    current = parsed_version(installed_version)
    target = parsed_version(target_version)
    if not current or not target:
        return "Unknown"

    current_numbers = current[:4]
    target_numbers = target[:4]

    if target_numbers[0] > current_numbers[0]:
        return "Major"
    if target_numbers[1] > current_numbers[1]:
        return "Minor"
    if target_numbers[2:] > current_numbers[2:]:
        return "Patch"
    if target > current:
        return "Prerelease"
    return "None"


def docker_image_created(image):
    code, output, _ = run(
        f"docker image inspect '{image}' --format='{{{{.Created}}}}'",
        timeout=20,
    )
    if code != 0 or not output:
        return None
    return parse_iso_timestamp(output)


def release_state(app_key, installed, releases):
    stable = releases.get("stable")
    beta = releases.get("beta")
    raw_version = installed.get("version")
    version = comparable_version(app_key, raw_version)

    # Floating Docker tags cannot always be compared safely without pulling.
    # Report them as requiring an explicit registry check rather than falsely
    # claiming that they are current or outdated.
    if installed.get("image", "").endswith(":latest") and not version:
        if installed.get("local_digest") and installed.get("remote_digest"):
            if installed["local_digest"] != installed["remote_digest"]:
                return "UPDATE", "Newer Docker image available", stable
            return "OK", "Docker image is current", stable
        return (
            "CHECK",
            "Floating :latest image; run the update assistant to check/pull",
            stable,
        )

    if not version:
        return "UNKNOWN", "Installed version could not be determined", None

    if not stable:
        return "UNKNOWN", "Latest stable release could not be determined", None

    stable_version = comparable_version(app_key, stable["version"])
    beta_version = comparable_version(app_key, beta["version"]) if beta else None

    if version_key(stable_version) > version_key(version):
        severity = update_severity(version, stable_version)
        return "UPDATE", f"{severity} stable update available", stable

    if beta and version_key(beta_version) > version_key(version):
        return "OK", "Up to date; newer prerelease also exists", stable

    return "OK", "Up to date", stable


def software_status():
    records = []

    for key, app in APP_CATALOG.items():
        installed = installed_app(key)
        if not installed:
            continue

        if installed.get("image"):
            installed["created"] = docker_image_created(installed["image"])

        releases = github_releases(app["repo"])
        state, message, target = release_state(key, installed, releases)
        severity = "None"
        if state == "UPDATE" and target and installed.get("version"):
            severity = update_severity(
                comparable_version(key, installed["version"]),
                comparable_version(key, target["version"]),
            )

        records.append({
            "key": key,
            "app": app,
            "installed": installed,
            "releases": releases,
            "state": state,
            "message": message,
            "target": target,
            "severity": severity,
        })

    return records


def check_application_versions():
    section("SOFTWARE UPDATE STATUS")

    records = software_status()
    if not records:
        info("No supported applications were detected")
        return

    counts = {"OK": 0, "UPDATE": 0, "CHECK": 0, "UNKNOWN": 0}
    severity_counts = {"Major": 0, "Minor": 0, "Patch": 0, "Prerelease": 0}

    for record in records:
        counts[record["state"]] = counts.get(record["state"], 0) + 1
        if record["state"] == "UPDATE":
            severity = record["severity"]
            severity_counts[severity] = severity_counts.get(severity, 0) + 1

    print(f"Applications checked: {len(records)}")
    print(f"Up to date:           {counts['OK']}")
    print(f"Updates available:    {counts['UPDATE']}")
    print(f"Registry checks:      {counts['CHECK']}")
    print(f"Unknown:              {counts['UNKNOWN']}")
    if counts["UPDATE"]:
        print(
            "Update severity:      "
            f"{severity_counts.get('Major', 0)} major, "
            f"{severity_counts.get('Minor', 0)} minor, "
            f"{severity_counts.get('Patch', 0)} patch"
        )
    print()

    for record in records:
        key = record["key"]
        app = record["app"]
        installed = record["installed"]
        releases = record["releases"]
        state = record["state"]
        target = record["target"]

        marker = {
            "OK": color("[OK]", Colors.GREEN),
            "UPDATE": color("[UPDATE]", Colors.YELLOW),
            "CHECK": color("[CHECK]", Colors.BLUE),
            "UNKNOWN": color("[UNKNOWN]", Colors.YELLOW),
        }[state]

        print(f"{marker} {app['name']}")
        print(
            f"  Installed: "
            f"{installed.get('version') or 'detected, version unknown'}"
        )
        print(f"  Source:    {installed['source']}")

        stable = releases.get("stable")
        beta = releases.get("beta")
        if stable:
            print(f"  Stable:    {stable['tag']}")
        if beta:
            print(f"  Prerelease: {beta['tag']}")

        created = installed.get("created")
        if created:
            print(f"  Image built: {created.isoformat()}")

        if installed.get("local_digest"):
            print(f"  Local image digest:  {installed['local_digest']}")
        if installed.get("remote_digest"):
            print(f"  Remote image digest: {installed['remote_digest']}")

        print(f"  Status:    {record['message']}")

        if state == "UPDATE":
            if target:
                release_type = "Prerelease" if target["beta"] else "Stable"
                print(f"  Target:    {target['tag']} ({release_type})")
                print(f"  Severity:  {record['severity']}")
            print(f"  Command:   node-doctor update {key} --dry-run")
        elif state == "CHECK":
            print(f"  Command:   node-doctor update {key} --dry-run")
        elif releases.get("error"):
            print(f"  Lookup:    {releases['error']}")

        print()


def backup_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def update_plan(app_key, installed, target):
    app = APP_CATALOG[app_key]
    timestamp = backup_timestamp()
    home_backup = HOME / "node-doctor-backups"

    plan = {
        "backup_dir": home_backup,
        "commands": [],
        "rollback": [],
        "automated": False,
        "estimated_downtime": "Unknown",
        "notes": [],
    }

    if installed["type"] == "docker-compose":
        compose_dir = docker_compose_dir(installed["container"])
        if not compose_dir:
            plan["notes"].append(
                "Docker Compose working directory could not be determined."
            )
            return plan

        backup_file = home_backup / f"{app_key}-compose-{timestamp}.tar.gz"
        plan.update({
            "automated": True,
            "estimated_downtime": "Usually under 2 minutes",
            "backup_file": backup_file,
            "compose_dir": compose_dir,
            "commands": [
                f"mkdir -p '{home_backup}'",
                (
                    f"tar -czf '{backup_file}' -C '{compose_dir}' "
                    "--exclude='.git' ."
                ),
                f"cd '{compose_dir}' && docker compose pull",
                f"cd '{compose_dir}' && docker compose up -d",
                (
                    "docker ps --filter "
                    f"name='{installed['container']}' "
                    "--format '{{.Names}}: {{.Status}}'"
                ),
            ],
            "rollback": [
                f"cd '{compose_dir}' && docker compose down",
                (
                    f"tar -xzf '{backup_file}' "
                    f"-C '{compose_dir}'"
                ),
                f"cd '{compose_dir}' && docker compose up -d",
            ],
        })
        if installed.get("image", "").endswith(":latest"):
            plan["notes"].append(
                "The pull command is the authoritative registry check for this "
                "floating :latest image. It may download a newer image."
            )
        return plan

    source_path = installed.get("path")
    backup_file = home_backup / f"{app_key}-{timestamp}.tar.gz"
    service = app.get("service")

    plan["backup_file"] = backup_file
    plan["estimated_downtime"] = "Depends on application and rebuild time"
    plan["notes"].append(
        "Planner only: automatic execution is disabled for binary/source "
        "applications until checksum/signature and application-specific "
        "rollback support are implemented."
    )

    if source_path:
        source = Path(source_path)
        backup_source = source.parent if source.is_file() else source
        plan["commands"].extend([
            f"mkdir -p '{home_backup}'",
            (
                f"tar -czf '{backup_file}' "
                f"-C '{backup_source.parent}' '{backup_source.name}'"
            ),
        ])

    if service:
        plan["commands"].append(f"sudo systemctl stop '{service}'")

    if target:
        plan["commands"].append(
            f"# Download official {target['tag']} release from {app['repo']}"
        )
        plan["commands"].append("# Verify checksum/signature before installation")
        plan["commands"].append("# Install using the application's documented method")

    if service:
        plan["commands"].append(f"sudo systemctl start '{service}'")
        plan["commands"].append(f"systemctl --no-pager status '{service}'")

    if service:
        plan["rollback"].append(f"sudo systemctl stop '{service}'")
    if source_path:
        plan["rollback"].append(
            f"# Restore the prior application from '{backup_file}'"
        )
    if service:
        plan["rollback"].append(f"sudo systemctl start '{service}'")

    return plan


def update_app(app_key, dry_run=False):
    app = APP_CATALOG[app_key]
    installed = installed_app(app_key)

    if not installed:
        print(f"{app['name']} was not detected.")
        return 2

    if installed.get("image"):
        installed["created"] = docker_image_created(installed["image"])

    releases = github_releases(app["repo"])
    state, message, target = release_state(app_key, installed, releases)
    plan = update_plan(app_key, installed, target)

    section(f"{app['name'].upper()} UPDATE PLAN")
    print(f"Application:    {app['name']}")
    print(f"Installed:      {installed.get('version') or 'unknown'}")
    print(f"Install type:   {installed['type']}")
    print(f"Official repo:  {app['repo']}")
    print(f"Status:         {message}")

    if installed.get("created"):
        print(f"Image built:    {installed['created'].isoformat()}")

    if target:
        release_type = "Prerelease" if target["beta"] else "Stable"
        print(f"Available:      {target['tag']}")
        print(f"Release type:   {release_type}")
        if installed.get("version"):
            severity = update_severity(
                comparable_version(app_key, installed["version"]),
                comparable_version(app_key, target["version"]),
            )
            print(f"Severity:       {severity}")

    print(f"Backup:         {plan.get('backup_file', 'Not determined')}")
    print(f"Rollback:       {'Planned' if plan['rollback'] else 'Not available'}")
    print(f"Automation:     {'Supported' if plan['automated'] else 'Planner only'}")
    print(f"Est. downtime:  {plan['estimated_downtime']}")

    for note in plan["notes"]:
        print(f"Note:           {note}")

    # For floating Docker tags, the explicit pull is itself the update check.
    actionable = state == "UPDATE" or (
        state == "CHECK" and installed["type"] == "docker-compose"
    )

    if not actionable:
        print()
        print("No applicable update action is currently required.")
        return 0

    print()
    print("Planned commands:")
    for index, command in enumerate(plan["commands"], 1):
        print(f"  {index}. {command}")

    if plan["rollback"]:
        print()
        print("Rollback plan:")
        for index, command in enumerate(plan["rollback"], 1):
            print(f"  {index}. {command}")

    if dry_run:
        print()
        print(color("Dry run complete. No changes were made.", Colors.BLUE))
        return 0

    if not plan["automated"]:
        print()
        print(color("[NOT AUTOMATED]", Colors.YELLOW))
        print(
            "No commands were run. Use this plan as a review checklist until "
            "the application-specific verified installer is implemented."
        )
        return 3

    answer = input(
        "\nProceed? This may pull a new image and briefly restart the app. [y/N]: "
    ).strip().lower()
    if answer not in ("y", "yes"):
        print("Update cancelled. No changes were made.")
        return 0

    completed = []
    for command in plan["commands"]:
        print()
        print(color(f"$ {command}", Colors.BOLD))
        code, stdout, stderr = run(command, timeout=900)
        if stdout:
            print(stdout)
        if stderr:
            print(stderr)
        if code != 0:
            print(color("Update stopped because a command failed.", Colors.RED))
            if completed:
                print(
                    "A backup was created. Review the rollback plan above "
                    "before restoring."
                )
            return 4
        completed.append(command)

    print()
    print(color("Update commands completed.", Colors.GREEN))
    print("Run node-doctor again to verify node health.")
    return 0


def parse_arguments():
    parser = argparse.ArgumentParser(prog="node-doctor")
    parser.add_argument(
        "--no-update-check",
        action="store_true",
        help="Skip online version checks during a normal health run.",
    )
    subparsers = parser.add_subparsers(dest="subcommand")
    subparsers.add_parser(
        "updates",
        help="Show installed and available versions.",
    )

    updater = subparsers.add_parser(
        "update",
        help="Launch the update assistant.",
    )
    updater.add_argument("application", choices=sorted(APP_CATALOG))
    updater.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the planned commands without changing anything.",
    )
    return parser.parse_args()


def check_system():
    section("SYSTEM")

    code, hostname, _ = run("hostname")
    print(f"Hostname: {hostname or socket.gethostname()}")

    code, uptime, _ = run("uptime -p")
    print(f"Uptime: {uptime or 'unknown'}")

    code, os_name, _ = run(
        ". /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\""
    )
    print(f"Operating system: {os_name or 'unknown'}")

    code, kernel, _ = run("uname -r")
    print(f"Kernel: {kernel or 'unknown'}")

    storage_score = 10
    code, disk_output, _ = run("df -P / | tail -1")
    if code == 0 and disk_output:
        fields = disk_output.split()
        try:
            usage = int(fields[4].rstrip("%"))
            free_kb = int(fields[3])
            free_gb = free_kb / 1024 / 1024

            print(f"Root disk usage: {usage}%")
            print(f"Root disk available: {free_gb:.1f} GB")

            if usage >= 95:
                failed(f"Root filesystem is critically full at {usage}%")
                storage_score = 0
            elif usage >= 90:
                failed(f"Root filesystem is {usage}% full")
                storage_score = 3
            elif usage >= 80:
                warned(f"Root filesystem is {usage}% full")
                storage_score = 7
            else:
                passed(f"Root filesystem usage is {usage}%")
        except (ValueError, IndexError):
            warned("Could not parse root filesystem usage")
            storage_score = 8
    else:
        warned("Could not read root filesystem usage")
        storage_score = 8

    COMPONENT_SCORES["Storage"] = (storage_score, 10)

    system_score = 10
    code, memory_output, _ = run("free -m")
    if code == 0:
        memory_line = next(
            (line for line in memory_output.splitlines() if line.startswith("Mem:")),
            None,
        )

        if memory_line:
            fields = memory_line.split()
            try:
                total_mb = int(fields[1])
                available_mb = int(fields[6])
                print(f"Memory total: {total_mb} MB")
                print(f"Memory available: {available_mb} MB")

                if available_mb < 300:
                    failed(f"Available memory is critically low: {available_mb} MB")
                    system_score = 0
                elif available_mb < 750:
                    warned(f"Available memory is low: {available_mb} MB")
                    system_score = 6
                else:
                    passed(f"Available memory is {available_mb} MB")
            except (ValueError, IndexError):
                warned("Could not parse memory information")
                system_score = 8
    else:
        warned("Could not read memory information")
        system_score = 8

    COMPONENT_SCORES["System"] = (system_score, 10)


def check_smart():
    section("DRIVE HEALTH")

    if not command_exists("smartctl"):
        info(
            "smartctl is not installed; install smartmontools to enable "
            "drive-health checks"
        )
        COMPONENT_SCORES["Drive Health"] = (0, 0)
        return

    code, root_source, error = run("findmnt -n -o SOURCE /")
    if code != 0 or not root_source:
        warned(f"Could not identify the root storage device: {error}")
        COMPONENT_SCORES["Drive Health"] = (7, 10)
        return

    device = root_source.strip()

    code, parent, _ = run(
        f"lsblk -no PKNAME '{device}' 2>/dev/null | head -1"
    )
    if parent:
        device = f"/dev/{parent.strip()}"

    print(f"Device: {device}")

    smartctl_path = shutil.which("smartctl") or "/usr/sbin/smartctl"

    code, output, error = run(
        f"sudo -n '{smartctl_path}' -H -A '{device}'",
        timeout=30,
    )

    if code != 0 and not output:
        combined_error = (error or "").lower()

        if (
            "password is required" in combined_error
            or "a password is required" in combined_error
            or "no tty present" in combined_error
        ):
            info("SMART requires elevated privileges.")
            print()
            print("Automatic SMART monitoring is currently unavailable")
            print("because sudo requires a password.")
            print()
            print("To enable it safely:")
            print()
            print("  1. Open a restricted sudoers file:")
            print()
            print(
                "     sudo visudo "
                "-f /etc/sudoers.d/node-doctor-smartctl"
            )
            print()
            print("  2. Add this line:")
            print()
            print(
                f"     {USER} ALL=(root) NOPASSWD: "
                f"{smartctl_path}"
            )
            print()
            print("  3. Save the file and verify it:")
            print()
            print("     sudo chmod 440 "
                  "/etc/sudoers.d/node-doctor-smartctl")
            print("     sudo visudo -c")
            print()
            print("  4. Run Node Doctor again:")
            print()
            print("     node-doctor")
            print()
            print(
                "Only smartctl will be allowed without a password."
            )

            COMPONENT_SCORES["Drive Health"] = (0, 0)
            return

        unsupported_markers = (
            "unsupported",
            "unknown usb bridge",
            "smart support is unavailable",
            "smart support is: unavailable",
            "device does not support smart",
            "unable to detect device type",
        )

        if any(marker in combined_error for marker in unsupported_markers):
            info("SMART data is unavailable for this storage device.")
            if error:
                print(f"Reason: {error}")
            COMPONENT_SCORES["Drive Health"] = (0, 0)
            return

        warned("SMART data could not be read.")
        if error:
            print(f"Detail: {error}")
        COMPONENT_SCORES["Drive Health"] = (7, 10)
        return

    overall_line = next(
        (
            line for line in output.splitlines()
            if "SMART overall-health self-assessment test result" in line
            or "SMART Health Status" in line
        ),
        "",
    )

    print(overall_line or "SMART overall result: not explicitly reported")

    temperature_lines = [
        line.strip() for line in output.splitlines()
        if (
            "Temperature_Celsius" in line
            or line.strip().startswith("Temperature:")
            or "Composite Temperature" in line
        )
    ]

    for line in temperature_lines[:2]:
        print(line)

    interesting = (
        "Percentage Used",
        "Media and Data Integrity Errors",
        "Reallocated_Sector_Ct",
        "Current_Pending_Sector",
        "Offline_Uncorrectable",
    )

    for label in interesting:
        match = next(
            (line.strip() for line in output.splitlines() if label in line),
            None,
        )
        if match:
            print(match)

    score = 10

    if any(token in overall_line.upper() for token in ("PASSED", "OK")):
        passed("Drive SMART overall-health check passed")
    elif any(token in overall_line.upper() for token in ("FAILED", "BAD")):
        failed("Drive SMART overall-health check failed")
        score = 0
    else:
        warned("SMART data was read, but the overall result was unclear")
        score = 7

    # Warn on commonly exposed ATA temperatures above 55 C.
    for line in temperature_lines:
        values = [
            int(token.rstrip("C"))
            for token in line.replace("°", "").split()
            if token.rstrip("C").isdigit()
        ]
        plausible = [value for value in values if 10 <= value <= 100]

        if plausible:
            temperature = plausible[-1]
            if temperature >= 65:
                failed(f"Drive temperature is critically high: {temperature} C")
                score = 0
            elif temperature >= 55:
                warned(f"Drive temperature is elevated: {temperature} C")
                score = min(score, 7)
            else:
                passed(f"Drive temperature appears normal: {temperature} C")
            break

    COMPONENT_SCORES["Drive Health"] = (score, 10)

def check_connectivity():
    section("CONNECTIVITY AND TIME")

    score = 5

    code, route, _ = run("ip route get 1.1.1.1 2>/dev/null | head -1")
    if code == 0 and route:
        passed("A default network route is available")
    else:
        failed("No usable default network route was detected")
        score = 0

    code, _, error = run(
        "curl --silent --show-error --fail "
        "--max-time 8 https://example.com/ "
        "--output /dev/null"
    )

    if code == 0:
        passed("Outbound HTTPS connectivity is working")
    else:
        failed(
            "Outbound HTTPS connectivity failed"
            + (f": {error}" if error else "")
        )
        score = 0

    code, timedate, _ = run(
        "timedatectl show "
        "--property=NTPSynchronized "
        "--property=TimeUSec "
        "--property=Timezone"
    )

    if code == 0 and timedate:
        values = {}

        for line in timedate.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()

        print(f"Timezone: {values.get('Timezone', 'unknown')}")
        print(f"System time: {values.get('TimeUSec', 'unknown')}")

        if values.get("NTPSynchronized", "").lower() == "yes":
            passed("System clock is synchronized with NTP")
        else:
            warned("System clock is not reporting NTP synchronization")
            score = min(score, 3)
    else:
        warned("Could not determine NTP synchronization status")
        score = min(score, 3)

    COMPONENT_SCORES["Connectivity"] = (score, 5)


def check_bitcoin():
    section("BITCOIN CORE")

    if not command_exists("bitcoin-cli"):
        failed("bitcoin-cli is not installed or not in PATH")
        COMPONENT_SCORES["Bitcoin Core"] = (0, 20)
        return

    code, version, _ = run("bitcoind --version | head -1")
    print(f"Version: {version or 'unknown'}")

    blockchain, error = parse_json_command("bitcoin-cli getblockchaininfo")

    if blockchain is None:
        failed(f"Could not connect to Bitcoin Core: {error}")
        COMPONENT_SCORES["Bitcoin Core"] = (0, 20)
        return

    blocks = blockchain.get("blocks")
    headers = blockchain.get("headers")
    ibd = blockchain.get("initialblockdownload")
    progress = blockchain.get("verificationprogress")
    pruned = blockchain.get("pruned")

    print(f"Blocks: {blocks}")
    print(f"Headers: {headers}")
    print(f"Initial block download: {ibd}")
    print(f"Verification progress: {progress}")
    print(f"Pruned: {pruned}")

    score = 20

    if ibd is False and blocks == headers:
        passed("Bitcoin Core is fully synchronized")
    else:
        warned("Bitcoin Core is not fully synchronized")
        score -= 8

    network, error = parse_json_command("bitcoin-cli getnetworkinfo")
    if network:
        peer_count = int(network.get("connections", 0))
        inbound = int(network.get("connections_in", 0))
        outbound = int(network.get("connections_out", 0))

        print(f"Peer connections: {peer_count}")
        print(f"Inbound peers: {inbound}")
        print(f"Outbound peers: {outbound}")
        print(f"Subversion: {network.get('subversion', 'unknown')}")
        print(f"Relay fee: {network.get('relayfee', 'unknown')}")

        if peer_count > 0 and outbound > 0:
            passed(
                f"Bitcoin Core has {peer_count} peers "
                f"({inbound} inbound, {outbound} outbound)"
            )
        elif peer_count > 0:
            warned("Bitcoin Core has peers but no outbound connections")
            score -= 5
        else:
            failed("Bitcoin Core has no peers")
            score = 0
    else:
        failed(f"Could not read Bitcoin network information: {error}")
        score = max(0, score - 8)

    COMPONENT_SCORES["Bitcoin Core"] = (max(0, score), 20)

def check_lnd():
    section("LND")

    if not command_exists("lncli"):
        failed("lncli is not installed or not in PATH")
        return

    code, version, _ = run("lnd --version")
    print(f"Version: {version or 'unknown'}")

    lnd_info, error = parse_json_command("lncli getinfo")

    if lnd_info is None:
        failed(f"Could not connect to LND: {error}")
        COMPONENT_SCORES["Lightning"] = (0, 15)
        return

    chain_sync = lnd_info.get("synced_to_chain")
    graph_sync = lnd_info.get("synced_to_graph")
    peers = int(lnd_info.get("num_peers", 0))
    active = int(lnd_info.get("num_active_channels", 0))
    inactive = int(lnd_info.get("num_inactive_channels", 0))
    pending = int(lnd_info.get("num_pending_channels", 0))
    block_height = lnd_info.get("block_height")

    print(f"Block height: {block_height}")
    print(f"Synced to chain: {chain_sync}")
    print(f"Synced to graph: {graph_sync}")
    print(f"Peers: {peers}")
    print(f"Active channels: {active}")
    print(f"Inactive channels: {inactive}")
    print(f"Pending channels: {pending}")

    score = 15

    if chain_sync:
        passed("LND is synchronized to the Bitcoin chain")
    else:
        failed("LND is not synchronized to the Bitcoin chain")
        score = 0

    if graph_sync:
        passed("LND graph synchronization is complete")
    else:
        warned("LND graph synchronization is incomplete")
        score = max(0, score - 3)

    if peers > 0:
        passed(f"LND has {peers} peers")
    elif active == 0 and inactive == 0 and pending == 0:
        info("LND has no peers, which is expected with no channels")
    else:
        warned("LND has no connected peers")
        score = max(0, score - 2)

    if active == 0 and inactive == 0 and pending == 0:
        print("Lightning mode: standalone node with no open channels")

    balance, error = parse_json_command("lncli walletbalance")
    if balance:
        print(f"Wallet balance: {balance.get('total_balance', 'unknown')} sats")

    COMPONENT_SCORES["Lightning"] = (score, 15)



def check_lndg():
    section("LNDg")

    container = "lndg-lndg-1"
    database = HOME / "lndg" / "data" / "db.sqlite3"

    if not command_exists("docker"):
        failed("LNDg cannot be checked because Docker is unavailable")
        return

    # Confirm the container exists and is running.
    code, running, error = run(
        f"docker inspect --format='{{{{.State.Running}}}}' {container}"
    )

    if code != 0:
        failed(f"LNDg Docker container was not found: {error or running}")
        return

    if running.strip() == "true":
        passed("LNDg Docker container is running")
    else:
        failed("LNDg Docker container is not running")
        return

    # Report the version contained in the running image.
    version_command = (
        f"docker exec {container} sh -c "
        "\"git -C /app describe --tags --always 2>/dev/null "
        "|| git -C /lndg describe --tags --always 2>/dev/null "
        "|| true\""
    )

    code, version, _ = run(version_command)

    if version:
        print(f"Version: {version}")
    else:
        warned("Could not determine the installed LNDg version")

    # Confirm the application is serving HTTP.
    code, http_status, error = run(
        "curl --silent --show-error "
        "--output /dev/null "
        "--write-out '%{http_code}' "
        "--max-time 10 "
        "http://127.0.0.1:8889/"
    )

    try:
        status_code = int(http_status.strip())
    except ValueError:
        status_code = 0

    if code == 0 and 200 <= status_code < 400:
        print(f"HTTP status: {status_code}")
        passed(f"LNDg web interface is responding with HTTP {status_code}")
    else:
        failed(
            "LNDg web interface is not responding on port 8889"
            + (f": {error}" if error else "")
        )

    # Check the persistent SQLite database.
    if not database.exists():
        failed(f"LNDg database was not found at {database}")
    elif not database.is_file():
        failed(f"LNDg database path is not a regular file: {database}")
    else:
        try:
            size_mb = database.stat().st_size / 1024 / 1024
            modified = datetime.datetime.fromtimestamp(
                database.stat().st_mtime
            ).astimezone()

            print(f"Database: {database}")
            print(f"Database size: {size_mb:.1f} MB")
            print(f"Database modified: {modified}")

            passed("LNDg database is present")

            import sqlite3

            connection = sqlite3.connect(
                f"file:{database}?mode=ro",
                uri=True,
                timeout=15,
            )

            try:
                rows = connection.execute(
                    "PRAGMA integrity_check;"
                ).fetchall()
            finally:
                connection.close()

            integrity_messages = [
                str(row[0]) for row in rows if row
            ]

            if integrity_messages == ["ok"]:
                passed("LNDg database integrity check passed")
            else:
                failed(
                    "LNDg database integrity check failed: "
                    + "; ".join(integrity_messages[:5])
                )

        except Exception as exc:
            failed(f"Could not verify the LNDg database: {exc}")

    # Confirm the LNDg container can reach the local LND gRPC service.
    grpc_command = (
        f"docker exec {container} python -c "
        "\"import socket; "
        "s=socket.create_connection(('127.0.0.1',10009),5); "
        "s.close()\""
    )

    code, _, error = run(grpc_command, timeout=10)

    if code == 0:
        passed("LNDg can reach the LND gRPC port")
    else:
        failed(
            "LNDg cannot reach the LND gRPC port"
            + (f": {error}" if error else "")
        )

    lndg_failures = [
        result for result in RESULTS
        if result.startswith("[FAIL] LNDg")
    ]
    COMPONENT_SCORES["LNDg"] = (0 if lndg_failures else 15, 15)


def service_exists(name):
    code, stdout, _ = run(
        f"systemctl list-unit-files --type=service --no-legend "
        f"'{name}.service' 2>/dev/null"
    )
    return bool(stdout.strip())


def check_services():
    section("SYSTEMD SERVICES")

    services = [
        "bitcoind",
        "lnd",
        "fulcrum",
        "thunderhub",
        "RTL",
        "lnbits",
        "pihole-FTL",
        "docker",
    ]

    failed_services = 0

    for service in services:
        if not service_exists(service):
            info(f"{service} service is not installed")
            continue

        code, status, _ = run(f"systemctl is-active {service}")
        status = status.strip()

        code, active_since, _ = run(
            f"systemctl show {service} "
            f"--property=ActiveEnterTimestamp --value"
        )

        if status == "active":
            suffix = f" since {active_since}" if active_since else ""
            passed(f"{service} is active{suffix}")
        else:
            failed(f"{service} is {status or 'not active'}")
            failed_services += 1

    COMPONENT_SCORES["Services"] = (
        max(0, 10 - failed_services * 3),
        10,
    )

def check_docker():
    section("DOCKER")

    if not command_exists("docker"):
        info("Docker is not installed")
        COMPONENT_SCORES["Docker"] = (0, 0)
        return

    code, _, error = run("docker info", timeout=30)
    if code != 0:
        failed(f"Docker daemon is unavailable: {error}")
        COMPONENT_SCORES["Docker"] = (0, 10)
        return

    code, output, error = run(
        "docker ps -a --format "
        "'{{.Names}}|{{.Image}}|{{.Status}}'"
    )

    if code != 0:
        failed(f"Could not list Docker containers: {error}")
        COMPONENT_SCORES["Docker"] = (0, 10)
        return

    containers = []

    for line in output.splitlines():
        if not line.strip():
            continue

        parts = line.split("|", 2)
        if len(parts) == 3:
            containers.append(parts)

    if not containers:
        warned("No Docker containers were found")
        COMPONENT_SCORES["Docker"] = (5, 10)
        return

    score = 10
    now = datetime.datetime.now(datetime.timezone.utc)

    for name, image, status in containers:
        code, inspection, error = run(
            f"docker inspect "
            f"--format='{{{{.State.Running}}}}|"
            f"{{{{.RestartCount}}}}|"
            f"{{{{.State.StartedAt}}}}' "
            f"'{name}'"
        )

        running = False
        restart_count = 0
        started_at = None

        if code == 0:
            parts = inspection.split("|", 2)

            if len(parts) == 3:
                running = parts[0].strip() == "true"

                try:
                    restart_count = int(parts[1].strip())
                except ValueError:
                    restart_count = 0

                started_at = parse_iso_timestamp(parts[2])

        print(f"{name}: {image}")
        print(f"  Status: {status}")

        if started_at is not None:
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=datetime.timezone.utc)

            uptime_seconds = (now - started_at.astimezone(
                datetime.timezone.utc
            )).total_seconds()

            print(f"  Started: {started_at.astimezone()}")
            print(f"  Current uptime: {human_age(uptime_seconds)}")
        else:
            print("  Current uptime: unknown")

        print(f"  Restarts: {restart_count}")

        if not running:
            failed(f"Docker container {name} is not running")
            score = max(0, score - 4)
        elif restart_count >= 5:
            warned(f"Docker container {name} has restarted {restart_count} times")
            score = max(0, score - 1)
        else:
            passed(f"Docker container {name} is running")

    code, docker_storage, _ = run("docker system df")
    if code == 0 and docker_storage:
        print()
        print("Docker storage:")
        print(docker_storage)

    COMPONENT_SCORES["Docker"] = (score, 10)

def check_backups():
    section("BACKUPS")

    backup_groups = {
        "Bitcoin": [
            str(HOME / "bitcoin-backup-*"),
        ],
        "LND": [
            str(HOME / "lnd-backup-*"),
            str(HOME / "lndbackup" / "*"),
            str(HOME / ".lnd_backup" / "*"),
        ],
        "LNDg": [
            str(HOME / "lndg-backup-*"),
            str(HOME / "lndg-backups" / "*"),
        ],
        "LNbits": [
            str(HOME / "lnbits-backup-*"),
            str(HOME / "lnbits-pre-*"),
        ],
        "RTL": [
            str(HOME / "RTL-backup-*"),
            str(HOME / "RTL-pre-*"),
        ],
        "ThunderHub": [
            str(HOME / "thunderhub-backup-*"),
            str(HOME / "thunderhub-untracked-*"),
        ],
    }

    now = datetime.datetime.now().timestamp()
    stale_count = 0
    found_count = 0

    for label, patterns in backup_groups.items():
        matches = []

        for pattern in patterns:
            matches.extend(glob.glob(pattern))

        existing = [
            Path(path) for path in matches
            if Path(path).exists()
        ]

        if not existing:
            info(f"{label}: no matching backup found")
            continue

        found_count += 1
        latest = max(existing, key=lambda path: path.stat().st_mtime)
        age_seconds = now - latest.stat().st_mtime
        age_days = age_seconds / 86400
        modified = datetime.datetime.fromtimestamp(
            latest.stat().st_mtime
        ).astimezone()

        print(f"{label}: {latest}")
        print(f"  Modified: {modified}")
        print(f"  Age: {human_age(age_seconds)}")

        if age_days <= 14:
            passed(f"{label} backup is recent")
        elif age_days <= 30:
            warned(f"{label} backup is {age_days:.0f} days old")
            stale_count += 1
        else:
            warned(f"{label} backup is stale at {age_days:.0f} days old")
            stale_count += 1

    if found_count == 0:
        COMPONENT_SCORES["Backups"] = (0, 0)
    else:
        COMPONENT_SCORES["Backups"] = (
            max(0, 5 - min(stale_count, 5)),
            5,
        )


def check_pihole():
    section("PI-HOLE")

    if not command_exists("pihole"):
        info("Pi-hole is not installed")
        return

    code, version, _ = run("pihole version")
    if version:
        print(version)

    code, status, _ = run("systemctl is-active pihole-FTL")
    if status == "active":
        passed("Pi-hole FTL is active")
    else:
        failed(f"Pi-hole FTL is {status or 'not active'}")

    code, listener, _ = run("ss -lntup | grep -E '[:.]53[[:space:]]'")
    if listener:
        passed("A DNS service is listening on port 53")
    else:
        failed("No DNS service is listening on port 53")

    if command_exists("dig"):
        code, dns_result, _ = run(
            "dig +time=3 +tries=1 +short google.com @127.0.0.1"
        )

        valid_result = any(
            part.replace(".", "").isdigit()
            for part in dns_result.splitlines()
        )

        if code == 0 and valid_result:
            passed("Pi-hole resolves DNS queries locally")
        else:
            failed("Pi-hole local DNS resolution failed")
    else:
        warned("dig is not installed, so DNS resolution was not tested")

    pihole_failures = [
        result for result in RESULTS
        if result.startswith("[FAIL] Pi-hole")
        or result.startswith("[FAIL] No DNS service")
    ]
    COMPONENT_SCORES["Pi-hole"] = (
        0 if pihole_failures else 5,
        5,
    )


def check_failed_units():
    section("FAILED SYSTEMD UNITS")

    code, output, error = run(
        "systemctl --failed --no-legend --no-pager"
    )

    failed_units = [
        line for line in output.splitlines()
        if line.strip()
    ]

    if not failed_units:
        passed("No failed systemd units")
    else:
        for line in failed_units:
            print(line)
        failed(f"{len(failed_units)} systemd unit(s) have failed")


def check_recent_errors():
    section("RECENT SERVICE ERRORS")

    services = ["bitcoind", "lnd", "pihole-FTL", "thunderhub", "lnbits"]

    found_errors = False

    for service in services:
        if not service_exists(service):
            continue

        code, output, _ = run(
            f"journalctl -u {service} "
            f"--since '24 hours ago' "
            f"-p err "
            f"--no-pager "
            f"--output=short-iso "
            f"2>/dev/null | tail -5"
        )

        meaningful_lines = [
            line for line in output.splitlines()
            if line.strip() and "No entries" not in line
        ]

        if meaningful_lines:
            found_errors = True
            print()
            print(f"{service}:")
            for line in meaningful_lines:
                print(f"  {line}")

    if found_errors:
        warned("Recent error-level journal entries were found")
    else:
        passed("No recent error-level journal entries were found")


def calculate_health_score():
    total_points = sum(
        points for points, maximum in COMPONENT_SCORES.values()
        if maximum > 0
    )
    total_maximum = sum(
        maximum for points, maximum in COMPONENT_SCORES.values()
        if maximum > 0
    )

    if total_maximum == 0:
        return 0

    return round((total_points / total_maximum) * 100)


def component_status():
    return [
        (name, points, maximum)
        for name, (points, maximum) in COMPONENT_SCORES.items()
        if maximum > 0
    ]

def diagnosis():
    failures = [
        result.removeprefix("[FAIL] ")
        for result in RESULTS
        if result.startswith("[FAIL]")
    ]

    warnings = [
        result.removeprefix("[WARN] ")
        for result in RESULTS
        if result.startswith("[WARN]")
    ]

    lines = []

    if not failures and not warnings:
        lines.extend([
            "Your node appears fully operational.",
            "Bitcoin Core and LND are synchronized.",
            "All required services and production Docker containers are running.",
            "Pi-hole is answering DNS requests correctly.",
            "No immediate action is recommended.",
        ])
        return lines

    if failures:
        lines.append(
            "Your node has one or more failures that should be investigated."
        )

        for failure in failures:
            lower = failure.lower()

            if "lndg web interface" in lower:
                lines.append(
                    "LNDg is running, but its web interface is unavailable. "
                    "Check: cd ~/lndg && docker compose logs --tail=100"
                )
            elif "lndg database integrity" in lower:
                lines.append(
                    "The LNDg SQLite database failed its integrity check. "
                    "Do not delete the database. Review your dated LNDg backup "
                    "before attempting recovery."
                )
            elif "lndg database was not found" in lower:
                lines.append(
                    "The persistent LNDg database is missing. Check the "
                    "~/lndg/data mount and your Docker Compose configuration."
                )
            elif "lndg docker container" in lower:
                lines.append(
                    "The LNDg container is unavailable. Check: "
                    "cd ~/lndg && docker compose ps && "
                    "docker compose logs --tail=100"
                )
            elif "lndg cannot reach" in lower:
                lines.append(
                    "LNDg cannot reach LND on port 10009. Confirm LND is "
                    "running and that LNDg still uses host networking."
                )
            elif "bitcoin" in lower and "synchron" in lower:
                lines.append(
                    "Bitcoin Core is not synchronized. Check internet access "
                    "and run: journalctl -u bitcoind -n 100 --no-pager"
                )
            elif "bitcoin core has no peers" in lower:
                lines.append(
                    "Bitcoin Core has no peers. Check network connectivity, "
                    "firewall rules, and Bitcoin Core logs."
                )
            elif "lnd" in lower and "synchron" in lower:
                lines.append(
                    "LND is not synchronized. Confirm Bitcoin Core is synced, "
                    "then check: journalctl -u lnd -n 100 --no-pager"
                )
            elif "docker container" in lower:
                lines.append(
                    "A Docker container is stopped. Inspect it with: "
                    "docker ps -a"
                )
            elif "pi-hole" in lower or "dns" in lower:
                lines.append(
                    "Pi-hole or DNS resolution failed. Check: "
                    "sudo pihole status"
                )
            elif "filesystem" in lower:
                lines.append(
                    "Disk usage is critically high. Remove unnecessary files "
                    "before Bitcoin Core or Docker runs out of space."
                )

    if warnings:
        if not failures:
            lines.append(
                "Your node is operational, but some non-critical warnings "
                "were detected."
            )

        lines.append("Review the warning section above when convenient.")

    return lines


def write_report():
    timestamp = datetime.datetime.now().astimezone().isoformat()
    score = calculate_health_score()

    if FAIL_COUNT > 0:
        overall = "FAIL"
    elif WARN_COUNT > 0:
        overall = "HEALTHY WITH WARNINGS"
    else:
        overall = "HEALTHY"

    report_lines = [
        "NODE DOCTOR REPORT",
        f"Generated: {timestamp}",
        f"Hostname: {socket.gethostname()}",
        "",
        f"OVERALL STATUS: {overall}",
        f"HEALTH SCORE: {score}/100",
        f"PASS: {PASS_COUNT}",
        f"WARN: {WARN_COUNT}",
        f"FAIL: {FAIL_COUNT}",
        "",
        "COMPONENTS",
        "----------",
    ]

    for name, points, maximum in component_status():
        report_lines.append(f"{name}: {points}/{maximum}")

    report_lines.extend([
        "",
        "DIAGNOSIS",
        "---------",
        *diagnosis(),
        "",
        "RESULTS",
        "-------",
        *RESULTS,
        "",
    ])

    REPORT_PATH.write_text(
        "\n".join(report_lines),
        encoding="utf-8",
    )


def final_summary():
    section("NODE HEALTH SUMMARY")

    score = calculate_health_score()

    if FAIL_COUNT > 0:
        overall_plain = "FAIL"
        overall = color(overall_plain, Colors.RED)
    elif WARN_COUNT > 0:
        overall_plain = "HEALTHY WITH WARNINGS"
        overall = color(overall_plain, Colors.YELLOW)
    else:
        overall_plain = "HEALTHY"
        overall = color(overall_plain, Colors.GREEN)

    if score >= 90:
        score_display = color(f"{score}/100", Colors.GREEN)
    elif score >= 70:
        score_display = color(f"{score}/100", Colors.YELLOW)
    else:
        score_display = color(f"{score}/100", Colors.RED)

    print(f"Overall status: {overall}")
    print(f"Health score:   {score_display}")
    print()

    print("Component scores:")

    for name, points, maximum in component_status():
        if points == maximum:
            symbol = color("[OK]", Colors.GREEN)
        elif points >= maximum * 0.7:
            symbol = color("[CHECK]", Colors.YELLOW)
        else:
            symbol = color("[FAIL]", Colors.RED)

        print(f"{symbol} {name}: {points}/{maximum}")

    print()
    print(f"PASS: {PASS_COUNT}")
    print(f"WARN: {WARN_COUNT}")
    print(f"FAIL: {FAIL_COUNT}")

    section("NODE DIAGNOSIS")

    for line in diagnosis():
        print(line)

    print()
    print(f"Report saved to: {REPORT_PATH}")



def executive_summary():
    score = calculate_health_score()

    if FAIL_COUNT > 0:
        status_plain = "FAIL"
        status_display = color(status_plain, Colors.RED)
        action = "Immediate attention is required."
    elif WARN_COUNT > 0:
        status_plain = "HEALTHY WITH WARNINGS"
        status_display = color(status_plain, Colors.YELLOW)
        action = "Review the warning sections below."
    else:
        status_plain = "HEALTHY"
        status_display = color(status_plain, Colors.GREEN)
        action = "No action required."

    section("EXECUTIVE SUMMARY")
    print(f"Node status: {status_display}")
    print(f"Health score: {score}/100")
    print()

    for name, points, maximum in component_status():
        if points == maximum:
            symbol = color("[OK]", Colors.GREEN)
        elif points >= maximum * 0.7:
            symbol = color("[CHECK]", Colors.YELLOW)
        else:
            symbol = color("[FAIL]", Colors.RED)

        print(f"{symbol} {name:<16} {points}/{maximum}")

    code, disk_output, _ = run("df -hP / | tail -1")
    if code == 0 and disk_output:
        fields = disk_output.split()
        if len(fields) >= 5:
            print()
            print(f"Disk free: {fields[3]} ({fields[4]} used)")

    print()
    print(action)


def main():
    args = parse_arguments()

    if args.subcommand == "updates":
        print(color("=" * 60, Colors.BOLD))
        print(color("BITCOIN NODE DOCTOR v2.4.1", Colors.BOLD))
        print(f"Generated: {datetime.datetime.now().astimezone()}")
        print(color("=" * 60, Colors.BOLD))
        check_application_versions()
        return 0

    if args.subcommand == "update":
        return update_app(args.application, dry_run=args.dry_run)

    print(color("=" * 60, Colors.BOLD))
    print(color("BITCOIN NODE DOCTOR v2.4.1", Colors.BOLD))
    print(f"Generated: {datetime.datetime.now().astimezone()}")
    print(color("=" * 60, Colors.BOLD))

    check_system()
    check_smart()
    check_connectivity()
    check_bitcoin()
    check_lnd()
    check_lndg()
    check_services()
    check_docker()
    check_backups()
    check_pihole()
    check_failed_units()
    check_recent_errors()

    if not args.no_update_check:
        check_application_versions()

    executive_summary()
    write_report()
    final_summary()

    if FAIL_COUNT > 0:
        return 2
    if WARN_COUNT > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
