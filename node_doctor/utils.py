import datetime
import json
import re
import shutil
import subprocess
import sys


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
