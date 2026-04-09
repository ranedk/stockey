import json
import os
import platform
import shutil
import subprocess  # nosec B603, B404
import sys
import urllib.request
from pathlib import Path


GO_CROND_REPO = "webdevops/go-crond"
GO_CROND_DEFAULT_VERSION = "22.9.1"


def install_requirements(folder, project_name):
    original_cwd = os.getcwd()
    os.chdir(folder)
    venv_path = f".x{project_name}"
    try:
        if not os.path.exists(venv_path):
            print(f"Creating virtual environment for {project_name}...")
            subprocess.check_call(
                [sys.executable, "-m", "venv", venv_path]
            )  # nosec B603, B404
        else:
            print(f"Virtual environment already exists for {project_name}.")

        requirements_path = "requirements.txt"
        if os.path.exists(requirements_path):
            print(f"Installing requirements for {project_name}...")
            subprocess.check_call(
                [
                    os.path.join(venv_path, "bin", "pip"),
                    "install",
                    "-r",
                    requirements_path,
                ]
            )  # nosec B603, B404
    except subprocess.CalledProcessError as e:
        print(f"Failed to install requirements for {project_name}: {e}")
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
    except Exception:
        pass
    return GO_CROND_DEFAULT_VERSION


def resolve_go_crond_install_dir():
    explicit = os.getenv("GO_CROND_INSTALL_DIR")
    if explicit:
        path = Path(explicit).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    preferred = Path("/usr/local/bin")
    if preferred.exists() and os.access(preferred, os.W_OK | os.X_OK):
        return preferred

    fallback = Path.home() / ".local" / "bin"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def install_go_crond():
    existing = shutil.which("go-crond")
    if existing:
        print(f"go-crond already installed at {existing}")
        return existing

    target_dir = resolve_go_crond_install_dir()
    version = discover_go_crond_version()
    go_os = map_go_crond_os()
    go_arch = map_go_crond_arch()
    download_url = (
        f"https://github.com/{GO_CROND_REPO}/releases/download/"
        f"{version}/go-crond.{go_os}.{go_arch}"
    )
    target_path = target_dir / "go-crond"
    print(f"Installing go-crond {version} from {download_url} -> {target_path}")
    with urllib.request.urlopen(download_url, timeout=60) as response:  # nosec B310
        target_path.write_bytes(response.read())
    target_path.chmod(0o755)
    if str(target_dir) not in os.getenv("PATH", "").split(":"):
        print(
            f"go-crond installed at {target_path}. "
            f"Add {target_dir} to PATH for direct shell usage."
        )
    return str(target_path)


original_dir = str(Path(__file__).resolve().parent)
project_name = "stockey"
SERVICES = ["notebooks", "live", "backtest", "data"]


def setup_env():
    install_requirements(original_dir, "stockey")
    install_go_crond()
    vscode_config(original_dir, "stockey")
    venv_config(original_dir, "stockey")
    # for service in SERVICES:
    #     copy_environment(original_dir, f"{original_dir}/{service}")


def main():
    setup_env()


if __name__ == "__main__":
    main()
