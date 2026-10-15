"""Start the workspace's local Ollama service without a visible console."""

import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[2]
try:
    with urllib.request.urlopen("http://127.0.0.1:11435/api/tags", timeout=3):
        print("Local models are already running.")
except OSError:
    executable = Path(
        shutil.which("ollama")
        or str(Path(os.environ["LOCALAPPDATA"]) / "Programs/Ollama/ollama.exe")
    )
    if not executable.is_file():
        raise SystemExit("Ollama executable was not found.")
    env = os.environ.copy()
    env.update(
        OLLAMA_HOST="127.0.0.1:11435",
        OLLAMA_MODELS=str(root / "runtime/models/ollama"),
        OLLAMA_NUM_PARALLEL="1",
        OLLAMA_MAX_LOADED_MODELS="1",
        OLLAMA_CONTEXT_LENGTH="8192",
    )
    with (
        (root / "runtime/ollama-embedding.out.log").open("ab") as stdout,
        (root / "runtime/ollama-embedding.err.log").open("ab") as stderr,
    ):
        process = subprocess.Popen(
            [str(executable), "serve"],
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    print(f"Started local models, PID {process.pid}.")
