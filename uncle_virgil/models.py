"""Speech model catalog and downloader.

Models are fetched once from the sherpa-onnx GitHub releases and then run
entirely on this computer. Downloading them is the only time the speech
pipeline touches the network.
"""

from __future__ import annotations

import logging
import tarfile
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class Model:
    key: str
    label: str
    url: str
    check: str  # a file that exists once the model is installed
    size_mb: int
    files: dict[str, str] | None = None  # role -> relative path, for ASR models
    kind: str = ""  # ASR family: "transducer" or "whisper"


def _whisper(size: str, mb: int) -> Model:
    d = f"sherpa-onnx-whisper-{size}"
    return Model(
        key=f"whisper-{size}",
        label=f"Whisper {size}",
        url=f"{_BASE}/asr-models/{d}.tar.bz2",
        check=f"{d}/{size}-tokens.txt",
        size_mb=mb,
        files={"encoder": f"{d}/{size}-encoder.int8.onnx", "decoder": f"{d}/{size}-decoder.int8.onnx", "tokens": f"{d}/{size}-tokens.txt"},
        kind="whisper",
    )


_PARAKEET = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"

ASR_MODELS: dict[str, Model] = {
    m.key: m
    for m in [
        Model(
            key="parakeet-tdt-0.6b-v2",
            label="Parakeet 0.6B (English, recommended)",
            url=f"{_BASE}/asr-models/{_PARAKEET}.tar.bz2",
            check=f"{_PARAKEET}/tokens.txt",
            size_mb=460,
            files={
                "encoder": f"{_PARAKEET}/encoder.int8.onnx",
                "decoder": f"{_PARAKEET}/decoder.int8.onnx",
                "joiner": f"{_PARAKEET}/joiner.int8.onnx",
                "tokens": f"{_PARAKEET}/tokens.txt",
            },
            kind="transducer",
        ),
        _whisper("tiny.en", 113),
        _whisper("base.en", 199),
        _whisper("small.en", 606),
    ]
}

VAD = Model("vad", "Voice activity detector", f"{_BASE}/asr-models/silero_vad.onnx", "silero_vad.onnx", 1)
SEGMENTATION = Model(
    "segmentation",
    "Speaker segmentation",
    f"{_BASE}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
    "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
    7,
)
EMBEDDING = Model(
    "embedding",
    "Voiceprint model",
    # "recongition" is how the upstream release tag is spelled.
    f"{_BASE}/speaker-recongition-models/nemo_en_titanet_small.onnx",
    "nemo_en_titanet_small.onnx",
    38,
)


class ModelManager:
    def __init__(self, models_dir: Path):
        self.dir = Path(models_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.progress: dict = {"active": False, "model": "", "done_mb": 0.0, "total_mb": 0.0, "error": ""}

    def path(self, relative: str) -> Path:
        return self.dir / relative

    def installed(self, model: Model) -> bool:
        return self.path(model.check).exists()

    def required(self, asr_key: str) -> list[Model]:
        return [VAD, SEGMENTATION, EMBEDDING, ASR_MODELS[asr_key]]

    def missing(self, asr_key: str) -> list[Model]:
        return [m for m in self.required(asr_key) if not self.installed(m)]

    def status(self, asr_key: str) -> dict:
        return {
            "ready": not self.missing(asr_key),
            "missing": [{"key": m.key, "label": m.label, "size_mb": m.size_mb} for m in self.missing(asr_key)],
            "asr_models": [{"key": m.key, "label": m.label, "size_mb": m.size_mb, "installed": self.installed(m)} for m in ASR_MODELS.values()],
            "progress": dict(self.progress),
        }

    def download_missing(self, asr_key: str, on_done=None) -> bool:
        """Start downloading in the background. Returns False if a download is already running."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self.progress.update(active=True, error="")
            self._thread = threading.Thread(target=self._run, args=(asr_key, on_done), daemon=True, name="model-download")
            self._thread.start()
            return True

    def _run(self, asr_key: str, on_done) -> None:
        try:
            for model in self.missing(asr_key):
                self._download(model)
        except Exception as e:
            log.exception("model download failed")
            self.progress["error"] = f"{type(e).__name__}: {e}"
        finally:
            self.progress["active"] = False
            if on_done:
                on_done()

    def _download(self, model: Model) -> None:
        name = model.url.rsplit("/", 1)[1]
        tmp = self.dir / (name + ".part")
        self.progress.update(model=model.label, done_mb=0.0, total_mb=float(model.size_mb))
        with httpx.stream("GET", model.url, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            if total := int(r.headers.get("content-length", 0)):
                self.progress["total_mb"] = total / 1e6
            done = 0
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    self.progress["done_mb"] = done / 1e6
        if name.endswith(".tar.bz2"):
            with tarfile.open(tmp) as tar:
                tar.extractall(self.dir, filter="data")
            tmp.unlink()
        else:
            tmp.replace(self.dir / name)
        if not self.installed(model):
            raise RuntimeError(f"{model.label} downloaded but {model.check} is missing")
