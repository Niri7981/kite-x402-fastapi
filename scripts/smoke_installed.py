"""Install a wheel in a clean environment and smoke-test its public CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="built wheel to install")
    wheel = parser.parse_args().wheel.resolve()
    if wheel.suffix != ".whl" or not wheel.is_file():
        parser.error(f"not a wheel file: {wheel}")

    with tempfile.TemporaryDirectory(prefix="kite-x402-installed-smoke-") as temp:
        workdir = Path(temp)
        environment_dir = workdir / "venv"
        uv = shutil.which("uv")
        if uv:
            subprocess.run(
                [uv, "venv", "--seed", "--python", sys.executable, str(environment_dir)],
                check=True,
            )
        else:
            venv.EnvBuilder(with_pip=True).create(environment_dir)
        scripts_dir = environment_dir / ("Scripts" if os.name == "nt" else "bin")
        python = scripts_dir / ("python.exe" if os.name == "nt" else "python")
        cli = scripts_dir / ("kite-x402-fastapi.exe" if os.name == "nt" else "kite-x402-fastapi")
        evaluator = scripts_dir / ("kite-x402-evaluate.exe" if os.name == "nt" else "kite-x402-evaluate")

        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        environment["PYTHONNOUSERSITE"] = "1"

        install_command = (
            [uv, "pip", "install", "--python", str(python), str(wheel)]
            if uv
            else [str(python), "-m", "pip", "install", str(wheel)]
        )
        subprocess.run(install_command, cwd=workdir, env=environment, check=True)

        probe = subprocess.run(
            [
                str(python),
                "-c",
                "import kite_x402_fastapi; print(kite_x402_fastapi.__file__)",
            ],
            cwd=workdir,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        module_path = Path(probe.stdout.strip()).resolve()
        if not module_path.is_relative_to(environment_dir.resolve()):
            raise RuntimeError(f"package imported outside the clean environment: {module_path}")
        if not cli.is_file():
            raise RuntimeError(f"installed CLI was not created: {cli}")
        if not evaluator.is_file():
            raise RuntimeError(f"installed evaluator CLI was not created: {evaluator}")

        evaluation = subprocess.run(
            [str(evaluator), "--format", "json"],
            cwd=workdir,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        report = json.loads(evaluation.stdout)
        if report.get("summary", {}).get("status") != "passed" or not any(
            case.get("id") == "replay_protection" and case.get("status") == "passed"
            for case in report.get("cases", [])
        ):
            raise RuntimeError(f"installed evaluator reported a conformance failure: {report.get('summary')}")

        with socket.socket() as probe_socket:
            probe_socket.bind(("127.0.0.1", 0))
            port = probe_socket.getsockname()[1]

        environment.update(
            {
                "PAY_TO": "0x1111111111111111111111111111111111111111",
                "UPSTREAM_URL": "https://upstream.invalid",
                "KITE_NETWORK": "mainnet",
                "PRICE_USD": "0.001",
                "PORT": str(port),
            }
        )
        process = subprocess.Popen(
            [str(cli)],
            cwd=workdir,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 15
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output = process.communicate()[0]
                    raise RuntimeError(f"installed CLI exited before starting:\n{output}")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                        payload = json.load(response)
                    if payload.get("ok") is True:
                        print("Installed package import, evaluator, CLI startup, and /healthz passed.")
                        return
                    raise RuntimeError(f"unexpected /healthz response: {payload}")
                except (urllib.error.URLError, TimeoutError, OSError) as error:
                    last_error = error
                    time.sleep(0.1)
            raise RuntimeError(f"installed CLI did not start within 15 seconds: {last_error}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.communicate()


if __name__ == "__main__":
    main()
