"""Speech model catalog and downloader.

Models are fetched once from the sherpa-onnx GitHub releases and then run
entirely on this computer. Downloading them is the only time the speech
pipeline touches the network.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tarfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
RETRIES = 5


@dataclass(frozen=True)
class Model:
    key: str
    label: str
    url: str
    check: str  # a file that exists once the model is installed
    size_mb: int
    files: dict[str, str] | None = None  # role -> relative path, for ASR models
    kind: str = ""  # ASR family: "transducer" or "whisper"
    sha256: str = ""  # of the downloaded file; a file that does not match is refused


def _whisper(size: str, mb: int, sha256: str = "") -> Model:
    d = f"sherpa-onnx-whisper-{size}"
    return Model(
        key=f"whisper-{size}",
        label=f"Whisper {size}",
        url=f"{_BASE}/asr-models/{d}.tar.bz2",
        check=f"{d}/{size}-tokens.txt",
        size_mb=mb,
        files={"encoder": f"{d}/{size}-encoder.int8.onnx", "decoder": f"{d}/{size}-decoder.int8.onnx", "tokens": f"{d}/{size}-tokens.txt"},
        kind="whisper",
        sha256=sha256,
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
            sha256="6fc4426b54aa549a0019b8a75b8dea59164e71790a37ac56a045ad57f915f714",
        ),
        _whisper("tiny.en", 113, "2bd6cf965c8bb3e068ef9fa2191387ee63a9dfa2a4e37582a8109641c20005dd"),
        _whisper("base.en", 199, "475bc7052ce299c007f6d5d5407ba8601f819a2867f6eecee510ed17df581542"),
        _whisper("small.en", 606, "0cdba2b8aaab69e04847f3427cc9709574112e67913a1a84b7fec3a8729faa9a"),
    ]
}

VAD = Model(
    "vad",
    "Voice activity detector",
    f"{_BASE}/asr-models/silero_vad.onnx",
    "silero_vad.onnx",
    1,
    sha256="9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
)
SEGMENTATION = Model(
    "segmentation",
    "Speaker segmentation",
    f"{_BASE}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
    "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
    7,
    sha256="24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
)
EMBEDDING = Model(
    "embedding",
    "Voiceprint model",
    # "recongition" is how the upstream release tag is spelled.
    f"{_BASE}/speaker-recongition-models/nemo_en_titanet_small.onnx",
    "nemo_en_titanet_small.onnx",
    38,
    sha256="ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e",
)


def models_dir(home: Path) -> Path:
    """Where speech models live: <home>/models, or UNCLE_VIRGIL_MODELS to share one copy between data folders."""
    return Path(os.environ.get("UNCLE_VIRGIL_MODELS") or Path(home) / "models")


class ModelManager:
    def __init__(self, models_dir: Path):
        self.dir = Path(models_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=20.0))
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

    def download_now(self, asr_key: str, report=print) -> None:
        """Download missing models in the foreground (for the command line), reporting progress."""
        for model in self.missing(asr_key):
            report(f"Downloading {model.label} ({model.size_mb} MB)…")
            self._download(model)
        report("All speech models are installed.")

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
        # Room for the download plus, for archives, what it unpacks to.
        needed = model.size_mb * 1e6 * (2.2 if name.endswith(".tar.bz2") else 1.1) - (tmp.stat().st_size if tmp.exists() else 0)
        free = shutil.disk_usage(self.dir).free
        if free < needed:
            raise RuntimeError(f"Not enough free disk space for {model.label}: {needed / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free")

        for attempt in range(1, RETRIES + 1):
            try:
                self._fetch(model.url, tmp)
                break
            except httpx.HTTPError as e:
                if attempt == RETRIES:
                    raise RuntimeError(f"{model.label} could not be downloaded ({e}). Try again; it resumes where it stopped.") from e
                log.warning("download of %s interrupted (%s); resuming", name, e)
                time.sleep(min(30, 2**attempt))

        if model.sha256 and sha256_of(tmp) != model.sha256:
            tmp.unlink()  # corrupted, or not the file it should be: never use it
            raise RuntimeError(f"{model.label} did not match its expected checksum and was deleted. Try the download again.")
        if name.endswith(".tar.bz2"):
            # Unpack beside the models, then move into place, so an interrupted unpack never looks installed.
            staging = self.dir / (name + ".unpacking")
            shutil.rmtree(staging, ignore_errors=True)
            with tarfile.open(tmp) as tar:
                tar.extractall(staging, filter="data")
            for entry in staging.iterdir():
                target = self.dir / entry.name
                if target.is_dir():
                    shutil.rmtree(target)
                entry.replace(target)
            staging.rmdir()
            tmp.unlink()
        else:
            tmp.replace(self.dir / name)
        if not self.installed(model):
            raise RuntimeError(f"{model.label} downloaded but {model.check} is missing")

    def _fetch(self, url: str, tmp: Path) -> None:
        """Download to `tmp`, continuing a previous partial download when the server allows it."""
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        with self.client.stream("GET", url, headers=headers) as r:
            if r.status_code == 416:  # already complete
                return
            r.raise_for_status()
            if r.status_code != 206:  # the server sent the whole file: start over
                have = 0
            if total := int(r.headers.get("content-length", 0)):
                self.progress["total_mb"] = (have + total) / 1e6
            with tmp.open("ab" if have else "wb") as f:
                done = have
                for chunk in r.iter_bytes():  # as it arrives, so an interruption loses nothing already received
                    f.write(chunk)
                    done += len(chunk)
                    self.progress["done_mb"] = done / 1e6


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()
