import json
import os
import shutil
import subprocess  # nosec B603, B404
import sys
from pathlib import Path


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


original_dir = str(Path(__file__).resolve().parent)
project_name = "stockey"
SERVICES = ["notebooks", "live", "backtest", "data"]


def setup_env():
    install_requirements(original_dir, "stockey")
    vscode_config(original_dir, "stockey")
    venv_config(original_dir, "stockey")
    # for service in SERVICES:
    #     copy_environment(original_dir, f"{original_dir}/{service}")


def main():
    setup_env()


if __name__ == "__main__":
    main()
