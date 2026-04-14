import json
import os
import platform
import shutil
import subprocess  # nosec B603, B404
import sys
import urllib.request
from pathlib import Path
import getpass


GO_CROND_REPO = "webdevops/go-crond"
GO_CROND_DEFAULT_VERSION = "22.9.1"
PROJECT_ROOT = Path(__file__).resolve().parent
CRON_LOG_DIR = PROJECT_ROOT / "logs" / "cron"
CRON_TEMPLATE_PATH = PROJECT_ROOT / "config" / "stockey.crontab.template"
GENERATED_CRONTAB_PATH = PROJECT_ROOT / "config" / "stockey.generated.crontab"


def _log(message: str) -> None:
    print(message, flush=True)


def install_requirements(folder, project_name):
    original_cwd = os.getcwd()
    os.chdir(folder)
    venv_path = f".x{project_name}"
    try:
        if not os.path.exists(venv_path):
            _log(f"Creating virtual environment for {project_name}...")
            subprocess.check_call(
                [sys.executable, "-m", "venv", venv_path]
            )  # nosec B603, B404
        else:
            _log(f"Virtual environment already exists for {project_name}.")

        requirements_path = "requirements.txt"
        if os.path.exists(requirements_path):
            _log(f"Installing requirements for {project_name}...")
            subprocess.check_call(
                [
                    os.path.join(venv_path, "bin", "pip"),
                    "install",
                    "-r",
                    requirements_path,
                ]
            )  # nosec B603, B404
    except subprocess.CalledProcessError as e:
        _log(f"Failed to install requirements for {project_name}: {e}")
    finally:
        os.chdir(original_cwd)


def vscode_config(folder, project_name):
    vscode_dir = os.path.join(folder, ".vscode")
    os.makedirs(vscode_dir, exist_ok=True)
    settings_path = os.path.join(vscode_dir, "settings.json")

    settings = {
        "python.defaultInterpreterPath": f"{folder}/.x{project_name}/bin/python",
        "python.analysis.extraPaths": [],
    }
    with open(settings_path, "w") as settings_file:
        json.dump(settings, settings_file, indent=4)


def venv_config(folder, project_name):
    venv_path = os.path.join(folder, ".venv")
    venv_command = f"source .x{project_name}/bin/activate"
    with open(venv_path, "w") as vfile:
        vfile.write(venv_command)


def copy_environment(from_dir, to_dir):
    shutil.copy(f"{from_dir}/.env", f"{to_dir}/.env")


def map_go_crond_os() -> str:
    system = platform.system().lower()
    mapping = {
        "linux": "linux",
        "darwin": "darwin",
    }
    if system not in mapping:
        raise RuntimeError(f"Unsupported OS for go-crond install: {platform.system()}")
    return mapping[system]


def map_go_crond_arch() -> str:
    machine = platform.machine().lower()
    mapping = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "arm",
        "armv6l": "arm",
    }
    if machine not in mapping:
        raise RuntimeError(f"Unsupported architecture for go-crond install: {platform.machine()}")
    return mapping[machine]


def discover_go_crond_version():
    version = os.getenv("GO_CROND_VERSION")
    if version:
        return version
    api_url = f"https://api.github.com/repos/{GO_CROND_REPO}/releases/latest"
    try:
        with urllib.request.urlopen(api_url, timeout=20) as response:  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
        tag_name = str(payload.get("tag_name") or "").strip()
        if tag_name:
            return tag_name
    except Exception as exc:
        _log(
            f"Could not discover latest go-crond release from GitHub; "
            f"falling back to {GO_CROND_DEFAULT_VERSION} ({exc.__class__.__name__}: {exc})"
        )
    return GO_CROND_DEFAULT_VERSION


def resolve_go_crond_install_dir():
    explicit = os.getenv("GO_CROND_INSTALL_DIR")
    if explicit:
        path = Path(explicit).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    return PROJECT_ROOT


def ensure_runtime_directories() -> None:
    CRON_LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"Ensured runtime directory: {CRON_LOG_DIR}")


def render_crontab() -> str:
    if not CRON_TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Missing cron template: {CRON_TEMPLATE_PATH}")
    stockey_user = os.getenv("STOCKEY_CRON_USER") or getpass.getuser()
    rendered = CRON_TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "{{STOCKEY_USER}}": stockey_user,
        "{{STOCKEY_DIR}}": str(PROJECT_ROOT),
        "{{LOG_DIR}}": str(CRON_LOG_DIR),
        "{{GENERATED_CRONTAB}}": str(GENERATED_CRONTAB_PATH),
    }
    for needle, value in replacements.items():
        rendered = rendered.replace(needle, value)
    GENERATED_CRONTAB_PATH.write_text(rendered, encoding="utf-8")
    GENERATED_CRONTAB_PATH.chmod(0o600)
    _log(f"Rendered cron file: {GENERATED_CRONTAB_PATH}")
    return str(GENERATED_CRONTAB_PATH)


def install_go_crond():
    target_dir = resolve_go_crond_install_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "go-crond"
    if target_path.exists() and os.access(target_path, os.X_OK):
        _log(f"go-crond already installed at {target_path}")
        return str(target_path)

    existing = shutil.which("go-crond")
    if existing:
        existing_path = Path(existing).expanduser().resolve()
        if existing_path != target_path.resolve():
            shutil.copy2(existing_path, target_path)
            target_path.chmod(0o755)
            _log(f"Copied existing go-crond from {existing_path} -> {target_path}")
            return str(target_path)
        _log(f"go-crond already installed at {existing_path}")
        return str(existing_path)

    version = discover_go_crond_version()
    go_os = map_go_crond_os()
    go_arch = map_go_crond_arch()
    download_url = (
        f"https://github.com/{GO_CROND_REPO}/releases/download/"
        f"{version}/go-crond.{go_os}.{go_arch}"
    )
    _log(f"Installing go-crond {version} from {download_url} -> {target_path}")
    with urllib.request.urlopen(download_url, timeout=60) as response:  # nosec B310
        target_path.write_bytes(response.read())
    target_path.chmod(0o755)
    _log(
        f"go-crond installed at {target_path}. "
        f"Run it as ./go-crond or add {target_dir} to PATH."
    )
    return str(target_path)


original_dir = str(PROJECT_ROOT)
project_name = "stockey"
SERVICES = ["notebooks", "live", "backtest", "data"]


def setup_env():
    install_requirements(original_dir, "stockey")
    ensure_runtime_directories()
    install_go_crond()
    render_crontab()
    vscode_config(original_dir, "stockey")
    venv_config(original_dir, "stockey")
    # for service in SERVICES:
    #     copy_environment(original_dir, f"{original_dir}/{service}")


def main():
    setup_env()


if __name__ == "__main__":
    main()
