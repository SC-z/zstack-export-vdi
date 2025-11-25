#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests

MIN_FREE_BYTES = 50 * 1024 * 1024 * 1024  # 50G
WORKDIR = Path(__file__).resolve().parent
DEFAULT_QEMU_IMG = WORKDIR / "bin" / "qemu-img"
DEFAULT_LIB_PATH = f"{WORKDIR}/lib:/opt/qemu-lib"
HOST = "http://localhost:8080/"  # ZStack API 地址
USER_NAME = "admin"  # ZStack 账号
USER_PASSWORD = "password"  # ZStack 密码（按需修改）


def log(msg: str) -> None:
    print(f"[export_vdi] {msg}")


def log_stage(msg: str) -> None:
    log(f"==== {msg} ====")


def die(msg: str) -> None:
    log(f"ERROR: {msg}")
    sys.exit(1)


def run_cmd(cmd, env=None, check=True, capture=True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        env=env,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        universal_newlines=True,
    )
    if check and result.returncode != 0:
        stdout = result.stdout.strip() if result.stdout else ""
        stderr = result.stderr.strip() if result.stderr else ""
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\nstdout: {stdout}\nstderr: {stderr}"
        )
    return result


class Config(object):
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        ssh_user: str,
        ssh_pass: str,
        workdir: Path,
        qemu_img: Path,
        lib_path: Optional[str],
        min_free_bytes: int,
    ) -> None:
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.ssh_user = ssh_user
        self.ssh_pass = ssh_pass
        self.workdir = workdir
        self.qemu_img = qemu_img
        self.lib_path = lib_path
        self.min_free_bytes = min_free_bytes


class ZStackClient(object):
    def __init__(self, host: str, username: str, password: str) -> None:
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.session_uuid = None  # type: Optional[str]

    def _headers(self) -> dict:
        if not self.session_uuid:
            die("session uuid is missing, login first")
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": "OAuth {}".format(self.session_uuid),
        }

    def login(self) -> str:
        payload = {"logInByAccount": {"password": sha512_hex(self.password), "accountName": self.username}}
        url = self.host + "/zstack/v1/accounts/login"
        headers = {"Content-Type": "application/json"}
        resp = requests.put(url, data=json.dumps(payload), headers=headers, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        self.session_uuid = body["inventory"]["uuid"]
        return self.session_uuid

    def get_vm_by_ip(self, target_ip: str) -> Optional[dict]:
        url = self.host + "/zstack/v1/vm-instances"
        params = {"q": "vmNics.ip={}".format(target_ip)}
        resp = requests.get(url, headers=self._headers(), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        inventories = data.get("inventories") or []
        if not inventories:
            return None
        return inventories[0]

    def start_vm(self, vm_uuid: str) -> None:
        url = self.host + "/zstack/v1/vm-instances/{}/actions".format(vm_uuid)
        payload = {"startVmInstance": {}}
        resp = requests.put(url, headers=self._headers(), data=json.dumps(payload), timeout=15)
        resp.raise_for_status()


class SSHRunner(object):
    def __init__(self, ssh_user: str, ssh_pass: str) -> None:
        self.ssh_user = ssh_user
        self.ssh_pass = ssh_pass

    def run(self, ip: str, remote_cmd: str, retries: int = 1, retry_interval: int = 30) -> None:
        for attempt in range(1, retries + 1):
            try:
                run_cmd(
                    [
                        "sshpass",
                        "-p",
                        self.ssh_pass,
                        "ssh",
                        "-o",
                        "StrictHostKeyChecking=no",
                        "-o",
                        "UserKnownHostsFile=/dev/null",
                        f"{self.ssh_user}@{ip}",
                        remote_cmd,
                    ]
                )
                return
            except Exception as exc:  # noqa: BLE001
                if attempt < retries:
                    log(f"{remote_cmd} attempt {attempt} failed, retry in {retry_interval}s: {exc}")
                    time.sleep(retry_interval)
                else:
                    die(f"{remote_cmd} failed after {retries} attempts: {exc}")


def sha512_hex(value: str) -> str:
    sha512 = hashlib.sha512()
    sha512.update(value.encode("utf-8"))
    return sha512.hexdigest()


def get_root_install_path(vm: dict) -> Optional[str]:
    for vol in vm.get("allVolumes", []):
        if vol.get("type") == "Root":
            return vol.get("installPath")
    return None


def require_command(cmd: str) -> None:
    if shutil.which(cmd) is None:
        die(f"missing required command: {cmd}")


def ensure_vm_running(client: ZStackClient, target_ip: str, vm_uuid: str, initial_state: str, max_wait: int = 180, settle_secs: int = 20) -> None:
    state = (initial_state or "").lower()
    if state == "running":
        if settle_secs:
            time.sleep(settle_secs)
        return

    log(f"VM state is '{initial_state}', attempting start via API")
    client.start_vm(vm_uuid)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        vm = client.get_vm_by_ip(target_ip)
        if not vm:
            continue
        state = (vm.get("state") or "").lower()
        if state == "running":
            if settle_secs:
                time.sleep(settle_secs)
            return
    die(f"VM {vm_uuid} did not reach Running state within {max_wait}s")


def ssh_fstrim(ip: str, runner: SSHRunner) -> None:
    log(f"running fstrim on {runner.ssh_user}@{ip}")
    runner.run(ip, "fstrim /", retries=3, retry_interval=30)


def ssh_shutdown(ip: str, runner: SSHRunner) -> None:
    log(f"shutting down via ssh init 0 on {runner.ssh_user}@{ip}")
    runner.run(ip, "init 0")


def build_qemu_env(lib_path: Optional[str]) -> dict:
    env = os.environ.copy()
    if lib_path:
        existing = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = lib_path if not existing else f"{lib_path}:{existing}"
    return env


def ensure_qemu_available(qemu_img_path: Path, env: dict) -> None:
    if not qemu_img_path.is_file():
        die(f"qemu-img not found: {qemu_img_path}")
    if not os.access(qemu_img_path, os.X_OK):
        die(f"qemu-img not executable: {qemu_img_path}")
    run_cmd([str(qemu_img_path), "--help"], env=env)


def ensure_free_space(path: Path, required_bytes: int, label: str) -> None:
    usage = shutil.disk_usage(path)
    if usage.free < required_bytes:
        die(
            f"not enough free space on mount for {label}: "
            f"required {required_bytes} bytes, available {usage.free} bytes"
        )


def qemu_img_info(qemu_img_path: Path, env: dict, image_path: str) -> dict:
    result = run_cmd([str(qemu_img_path), "info", "--output", "json", image_path], env=env)
    return json.loads(result.stdout)


def convert_image(qemu_img_path: Path, env: dict, src: str, dst: str, src_fmt: str, dst_fmt: str) -> None:
    run_cmd(
        [
            str(qemu_img_path),
            "convert",
            "-f",
            src_fmt,
            "-O",
            dst_fmt,
            src,
            dst,
        ],
        env=env,
    )


def md5_file(path: Path) -> str:
    md5 = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export VM disk to qcow2 and vdi")
    parser.add_argument("ip", help="target VM IP address")
    parser.add_argument(
        "product",
        nargs="?",
        help="product name for output files (default: VM name from API)",
    )
    parser.add_argument(
        "--ssh-pass",
        default="letsg0",
        help="SSH password for root (default: letsg0)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    cfg = Config(
        host=HOST,
        username=USER_NAME,
        password=USER_PASSWORD,
        ssh_user="root",
        ssh_pass=args.ssh_pass,
        workdir=WORKDIR,
        qemu_img=DEFAULT_QEMU_IMG,
        lib_path=DEFAULT_LIB_PATH,
        min_free_bytes=MIN_FREE_BYTES,
    )

    require_command("ssh")
    require_command("sshpass")

    log_stage("环境准备")
    log(f"using workdir: {cfg.workdir}")
    log(f"using qemu-img: {cfg.qemu_img}")

    client = ZStackClient(cfg.host, cfg.username, cfg.password)
    session_uuid = client.login()
    log(f"login success, session uuid: {session_uuid}")

    log_stage("获取 VM 信息")
    vm = client.get_vm_by_ip(args.ip)
    if not vm:
        die(f"VM with IP {args.ip} not found")

    vm_uuid = vm.get("uuid")
    install_path = get_root_install_path(vm)
    vm_state = vm.get("state", "")
    product_name = args.product or vm.get("name") or "vm_image"
    if not vm_uuid or not install_path:
        die(f"VM with IP {args.ip} missing uuid or Root installPath")

    install_path = install_path.strip()
    log(f"found vm uuid: {vm_uuid}")
    log(f"Root installPath: {install_path}")
    log(f"product name: {product_name}")

    if not Path(install_path).exists():
        die(f"installPath not found locally: {install_path}")

    log_stage("启动与瘦身")
    ensure_vm_running(client, args.ip, vm_uuid, vm_state or "")
    ssh_runner = SSHRunner(cfg.ssh_user, cfg.ssh_pass)
    ssh_fstrim(args.ip, ssh_runner)
    ssh_shutdown(args.ip, ssh_runner)

    log_stage("镜像导出与转换")
    qemu_env = build_qemu_env(cfg.lib_path)
    ensure_qemu_available(cfg.qemu_img, qemu_env)

    image_info = qemu_img_info(cfg.qemu_img, qemu_env, install_path)
    actual_size = int(image_info.get("actual-size", 0))
    ensure_free_space(cfg.workdir, cfg.min_free_bytes, "pre-export check (>=50G)")
    if actual_size and shutil.disk_usage(cfg.workdir).free < actual_size:
        die("free space is smaller than source image actual size, aborting")

    qcow2_path = cfg.workdir / f"{product_name}.qcow2"
    vdi_path = cfg.workdir / f"{product_name}.vdi"
    if qcow2_path.exists() or vdi_path.exists():
        die(f"output file already exists: {qcow2_path} or {vdi_path}")

    log("exporting to qcow2")
    convert_image(cfg.qemu_img, qemu_env, install_path, str(qcow2_path), "qcow2", "qcow2")
    qcow2_size = qcow2_path.stat().st_size
    log(f"qcow2 output: {qcow2_path} (size: {qcow2_size} bytes)")

    ensure_free_space(cfg.workdir, max(qcow2_size, cfg.min_free_bytes), "vdi conversion stage")
    log("converting qcow2 to vdi")
    convert_image(cfg.qemu_img, qemu_env, str(qcow2_path), str(vdi_path), "qcow2", "vdi")
    log(f"vdi output: {vdi_path} (size: {vdi_path.stat().st_size} bytes)")

    log("generating vdi md5")
    vdi_md5 = md5_file(vdi_path)
    md5_path = vdi_path.with_suffix(vdi_path.suffix + ".md5")
    md5_path.write_text(f"{vdi_md5}  {vdi_path.name}\n", encoding="utf-8")
    log(f"md5 saved to {md5_path}")

    try:
        qcow2_path.unlink()
        log(f"removed intermediate qcow2: {qcow2_path}")
    except Exception as exc:  # noqa: BLE001
        log(f"warn: failed to remove {qcow2_path}: {exc}")

    log("flow completed")


if __name__ == "__main__":
    main()
