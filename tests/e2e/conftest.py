import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parents[2]
PROJECT = "flowforge-e2e"
SHIPPED_CONFIG = (ROOT / "config" / "pipeline.toml").read_text()
SHIPPED_STAGES = '["inventory", "fraud", "shipping"]'
TERMINAL_STATES = {"completed", "failed", "compensation_failed"}

OVERRIDES = {
    "fraud": {"max_attempts": 6, "dispatch_deadline_seconds": 5},
    "shipping": {"backoff_base_seconds": 8},
}


assert f"stages = {SHIPPED_STAGES}" in SHIPPED_CONFIG


def render_config(stages: list[str], overrides: dict[str, dict[str, float]]) -> str:
    quoted = ", ".join(f'"{stage}"' for stage in stages)
    text = SHIPPED_CONFIG.replace(f"stages = {SHIPPED_STAGES}", f"stages = [{quoted}]")
    for stage, values in overrides.items():
        text += f"\n[overrides.{stage}]\n" + "".join(
            f"{key} = {value}\n" for key, value in values.items()
        )
    return text


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Stack:
    def __init__(self, config_path: Path, port: int) -> None:
        self.config_path = config_path
        self.base_url = f"http://127.0.0.1:{port}"
        self._env = {
            **os.environ,
            "E2E_PIPELINE_CONFIG": str(config_path),
            "ORCHESTRATOR_PORT": str(port),
        }

    def compose(self, *args: str, gift_wrap: bool = False, check: bool = True):
        files = ["docker-compose.yml", "tests/e2e/compose.e2e.yml"]
        if gift_wrap:
            files.append("tests/e2e/compose.gift-wrap.yml")
        command = ["docker", "compose", "-p", PROJECT]
        for file in files:
            command += ["-f", file]
        run = subprocess.run(
            [*command, *args],
            cwd=ROOT,
            env=self._env,
            capture_output=True,
            text=True,
            timeout=900,
        )
        if check and run.returncode:
            raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{run.stderr}")
        return run

    def configure(self, stages=("inventory", "fraud", "shipping"), overrides=OVERRIDES) -> None:
        self.config_path.write_text(render_config(list(stages), overrides))

    def restart(self, service: str) -> None:
        self.compose("restart", service)
        if service == "orchestrator":
            self.wait_until_serving()

    def wait_until_serving(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.base_url}/", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise TimeoutError("the orchestrator did not come back")

    def logs(self, service: str) -> str:
        return self.compose("logs", "--no-log-prefix", service).stdout


class Orders:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def submit(self, script=None, payload=None, key=None) -> httpx.Response:
        body = {"payload": payload if payload is not None else {"sku": "A-1"}}
        if script is not None:
            body["script"] = script
        return self._client.post(
            "/api/orders", json=body, headers={"Idempotency-Key": key or uuid.uuid4().hex}
        )

    def start(self, script=None, payload=None) -> str:
        response = self.submit(script, payload)
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def status(self, order_id: str) -> dict:
        response = self._client.get(f"/api/orders/{order_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def wait_until(self, order_id: str, condition, timeout: float = 90) -> dict:
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            try:
                last = self.status(order_id)
            except httpx.HTTPError:
                time.sleep(0.5)
                continue
            if condition(last):
                return last
            time.sleep(0.25)
        raise TimeoutError(f"order {order_id} never reached the expected state; last: {last}")

    def finish(self, order_id: str, timeout: float = 90) -> dict:
        return self.wait_until(order_id, lambda s: s["state"] in TERMINAL_STATES, timeout)

    @staticmethod
    def wait_until_logged(stack: Stack, service: str, text: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in stack.logs(service):
                return
            time.sleep(0.5)
        raise TimeoutError(f"{service} never logged {text!r}")

    @staticmethod
    def history(status: dict) -> list[tuple]:
        return [(a["stage"], a["kind"], a["attempt"], a["outcome"]) for a in status["attempts"]]


@pytest.fixture(scope="session")
def stack(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("e2e-config") / "pipeline.toml"
    stack = Stack(config_path, free_port())
    stack.configure()
    try:
        stack.compose("up", "-d", "--build", "--wait")
        yield stack
    finally:
        stack.compose("down", "-v", "--remove-orphans", check=False)


@pytest.fixture
def orders(stack):
    with httpx.Client(base_url=stack.base_url, timeout=10) as client:
        yield Orders(client)


@pytest.fixture
def default_config(stack):
    yield
    stack.configure()
    stack.restart("orchestrator")
