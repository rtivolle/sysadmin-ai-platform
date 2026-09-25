#!/usr/bin/env python3
"""Ubuntu native GPU setup. Default is a read-only plan; --apply opts in."""
import argparse
import glob
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess


def has_nvidia():
    return any(Path(p).read_text().strip() == "0x10de"
               for p in glob.glob("/sys/bus/pci/devices/*/vendor"))


def commands(driver="auto", toolkit=None):
    if driver != "auto" and not re.fullmatch(r"[0-9]{3}(-server)?(-open)?", driver):
        raise ValueError("driver must be auto or an Ubuntu branch such as 570-server")
    if toolkit and not re.fullmatch(r"[0-9]{2}-[0-9]+", toolkit):
        raise ValueError("CUDA toolkit version must look like 12-8")
    result = [
        ["apt-get", "update"],
        ["apt-get", "install", "-y", "ubuntu-drivers-common", "pciutils", "mokutil",
         "build-essential", "dkms", f"linux-headers-{platform.release()}"],
        ["ubuntu-drivers", "list", "--gpgpu"],
        ["ubuntu-drivers", "install", "--gpgpu"],
    ]
    if driver != "auto":
        result[-1].append(f"nvidia:{driver}")
    if toolkit:
        result.append(["apt-get", "install", "-y", f"cuda-toolkit-{toolkit}"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--driver", default="auto")
    parser.add_argument("--cuda-toolkit", metavar="MAJOR-MINOR")
    args = parser.parse_args(argv)
    try:
        plan = commands(args.driver, args.cuda_toolkit)
    except ValueError as exc:
        parser.error(str(exc))
    distro = platform.freedesktop_os_release()
    print(f"OS: {distro.get('PRETTY_NAME')}; NVIDIA PCI device: {has_nvidia()}")
    for command in plan:
        print(shlex.join(command))
    print("After driver selection: install matching nvidia-utils-<branch> (includes nvidia-smi).")
    if not args.apply:
        print("Plan only. Use --apply to retrieve and install packages from configured signed repositories.")
        return 0
    if distro.get("ID") != "ubuntu" or not has_nvidia():
        parser.error("automatic installation requires Ubuntu and NVIDIA PCI hardware")
    if args.cuda_toolkit:
        print("CUDA toolkit requires an already configured NVIDIA signed APT repository.")
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    for command in plan:
        subprocess.run(prefix + command, check=True)
    # Match utilities to the installed driver package, including server variants.
    installed = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Package} ${Status}\n", "nvidia-driver-*"], text=True)
    branches = re.findall(r"^nvidia-driver-(\d+(?:-server)?)(?:-open)? install ok installed$",
                          installed, re.MULTILINE)
    if not branches:
        raise RuntimeError("No installed NVIDIA driver package found after installation")
    subprocess.run(prefix + ["apt-get", "install", "-y"] +
                   [f"nvidia-utils-{b}" for b in sorted(set(branches))], check=True)
    print("Packages installed. Reboot and complete Secure Boot MOK enrollment if requested.")
    print("Then verify nvidia-smi and the vLLM PyTorch CUDA probe before starting models.")
    if shutil.which("mokutil"):
        subprocess.run(["mokutil", "--sb-state"], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
