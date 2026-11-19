import json
import subprocess
from pathlib import Path


class Runtime:
    """One local bridge, one bounded execution slot; never retries model work."""

    def __init__(self, binary: str, state: Path, backend: str, codex: str):
        self.log = (state / "runtime-status.jsonl").open("a")
        self.process = subprocess.Popen(
            [binary, "--state", str(state / "runtime"), "--backend", backend,
             "--codex", codex],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, encoding="utf-8", bufsize=1,
        )

    def execute(self, request: dict) -> dict:
        encoded = json.dumps(request, ensure_ascii=False)
        if len(encoded.encode()) > 120 * 1024:
            raise ValueError("request exceeds bridge input limit")
        self.process.stdin.write(encoded + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline(1024 * 1024)
        if not line.endswith("\n"):
            raise RuntimeError("bridge closed or returned oversized response; inspect runtime-status.jsonl")
        result = json.loads(line)
        if result.get("id") != request["id"]:
            raise RuntimeError("bridge response ID mismatch")
        if result.get("status") != "succeeded":
            raise RuntimeError(result.get("error", "runtime task failed"))
        return result

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.log.close()
