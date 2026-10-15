"""Optional, pinned multilingual ONNX encoder for small cloud demo instances."""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.request
from pathlib import Path

MODEL = "Xenova/paraphrase-multilingual-MiniLM-L12-v2"
REVISION = "2c4055b12046f11709e9df2c122e59ffbdc2f900"
MODEL_SHA256 = "66fc00f5f29afcaff34092e1bdd20008ca3918265a82fb9695a551e510cc4ebc"
FILES = {"model.onnx": "onnx/model_quantized.onnx", "tokenizer.json": "tokenizer.json"}
_lock = threading.RLock()
_models = {}


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bundle_digest(directory):
    hashes = {name: file_hash(Path(directory) / name) for name in FILES}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def prepare_model(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for local, remote in FILES.items():
        destination = directory / local
        if not destination.is_file():
            temporary = destination.with_suffix(destination.suffix + ".partial")
            url = f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{remote}"
            with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
                total = 0
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    total += len(block)
                    if total > (160 * 1024 * 1024 if local == "model.onnx" else 50 * 1024 * 1024):
                        raise ValueError("Cloud embedding asset exceeds the size limit.")
                    output.write(block)
            temporary.replace(destination)
    if file_hash(directory / "model.onnx") != MODEL_SHA256:
        raise ValueError("Cloud embedding checksum mismatch.")
    return bundle_digest(directory)


class OnnxEncoder:
    def __init__(self, config):
        from .hybrid import IndexUnavailable

        self.config = config
        self.directory = Path(config["model_directory"]).resolve()
        if config.get("model") != MODEL or config.get("model_revision") != REVISION or config.get("dimensions") != 384:
            raise IndexUnavailable("Cloud embedding model/configuration is not the pinned profile.")

    def verify_model(self):
        from .hybrid import IndexUnavailable

        try:
            signatures = tuple((self.directory / name).stat().st_mtime_ns for name in FILES)
        except OSError:
            raise IndexUnavailable("Cloud embedding files are missing; rebuild the model bundle.") from None
        key = (str(self.directory), self.config["digest"])
        with _lock:
            cached = _models.get(key)
            if cached and cached[0] == signatures:
                return
            if bundle_digest(self.directory) != self.config["digest"] or file_hash(self.directory / "model.onnx") != MODEL_SHA256:
                raise IndexUnavailable("Cloud embedding files changed; verify and reindex before use.")
            import onnxruntime as ort
            from tokenizers import Tokenizer

            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.enable_cpu_mem_arena = False
            options.enable_mem_pattern = False
            options.add_session_config_entry("session.disable_prepacking", "1")
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            session = ort.InferenceSession(str(self.directory / "model.onnx"), sess_options=options, providers=["CPUExecutionProvider"])
            tokenizer = Tokenizer.from_file(str(self.directory / "tokenizer.json"))
            tokenizer.enable_truncation(max_length=128)
            _models[key] = (signatures, session, tokenizer)

    def embed(self, texts, query=False):
        if not texts:
            return []
        from .hybrid import unit_vector
        import numpy as np

        self.verify_model()
        result = []
        with _lock:
            _, session, tokenizer = _models[(str(self.directory), self.config["digest"])]
            names = {item.name for item in session.get_inputs()}
            for text in texts:
                encoded = tokenizer.encode(text)
                inputs = {
                    "input_ids": np.asarray([encoded.ids], dtype=np.int64),
                    "attention_mask": np.asarray([encoded.attention_mask], dtype=np.int64),
                    "token_type_ids": np.asarray([encoded.type_ids], dtype=np.int64),
                }
                output = session.run(None, {name: value for name, value in inputs.items() if name in names})[0]
                if output.ndim == 3:
                    mask = inputs["attention_mask"][..., None].astype(np.float32)
                    vector = (output * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1)
                else:
                    vector = output
                result.append(unit_vector(vector[0].tolist(), 384))
        return result
