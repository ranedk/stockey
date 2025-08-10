import os
import subprocess
from environs import Env


env = Env()
env.read_env()


def restart_chrome():
    os.system("pkill chrome")
    os.system("rm -rf ./chromesetup")
    os.system("cp -R ./base_chromed_data ./chromesetup")
    time.sleep(10)
    subprocess.Popen([env("CHROME_BINARY"), "--remote-debugging-port=9222", "--user-data-dir=./chromesetup"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
