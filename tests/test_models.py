"""Model downloads: resume after interruption, refuse corrupted files, check disk space."""

import hashlib
import io
import os
import tarfile
from collections import namedtuple

import httpx
import pytest

from uncle_virgil import models
from uncle_virgil.models import Model, ModelManager


def archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FlakyServer:
    """Serves one file; honors Range requests; can drop the connection part-way through."""

    def __init__(self, data: bytes, fail_after: list[int] = (), ranges: bool = True):
        self.data, self.fail_after, self.ranges = data, list(fail_after), ranges
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        start = 0
        if self.ranges and (r := request.headers.get("range")):
            start = int(r.split("=")[1].rstrip("-"))
        self.requests.append(start)
        body = self.data[start:]
        cut = self.fail_after.pop(0) if self.fail_after else None

        def stream():
            if cut is None:
                yield body
                return
            yield body[:cut]
            raise httpx.ReadError("connection dropped")

        status = 206 if start else 200
        return httpx.Response(status, headers={"content-length": str(len(body))}, stream=_Gen(stream()))


class _Gen(httpx.SyncByteStream):
    def __init__(self, gen):
        self.gen = gen

    def __iter__(self):
        yield from self.gen


def manager(tmp_path, server) -> ModelManager:
    m = ModelManager(tmp_path / "models")
    m.client = httpx.Client(transport=httpx.MockTransport(server))
    return m


def model_for(data: bytes, name="m.tar.bz2", check="m/a.onnx", sha=None) -> Model:
    return Model("m", "Test model", f"https://example.test/{name}", check, 1, sha256=sha if sha is not None else hashlib.sha256(data).hexdigest())


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(models.time, "sleep", lambda s: None)


def test_an_interrupted_download_resumes_where_it_stopped(tmp_path):
    payload = os.urandom(300_000)  # random, so the archive does not shrink
    data = archive({"m/a.onnx": payload})
    server = FlakyServer(data, fail_after=[100_000, 50_000])
    m = manager(tmp_path, server)
    m._download(model_for(data))
    assert server.requests == [0, 100_000, 150_000]  # each retry asks only for the rest
    assert (tmp_path / "models" / "m" / "a.onnx").read_bytes() == payload
    assert not list((tmp_path / "models").glob("*.part"))  # archive removed once unpacked


def test_a_download_stopped_by_closing_the_app_continues_next_time(tmp_path):
    data = archive({"m/a.onnx": os.urandom(500_000)})
    m = manager(tmp_path, FlakyServer(data, fail_after=[80_000] * models.RETRIES))
    with pytest.raises(RuntimeError, match="resumes where it stopped"):
        m._download(model_for(data))
    server = FlakyServer(data)
    m.client = httpx.Client(transport=httpx.MockTransport(server))
    m._download(model_for(data))
    assert server.requests == [80_000 * models.RETRIES]


def test_a_server_that_ignores_resume_requests_gets_a_clean_restart(tmp_path):
    data = b"z" * 120_000
    server = FlakyServer(data, fail_after=[60_000], ranges=False)
    m = manager(tmp_path, server)
    m._download(model_for(data, name="v.onnx", check="v.onnx"))
    assert (tmp_path / "models" / "v.onnx").read_bytes() == data  # not 60 000 bytes twice


def test_a_corrupted_or_substituted_file_is_refused_and_deleted(tmp_path):
    data = archive({"m/a.onnx": b"good"})
    m = manager(tmp_path, FlakyServer(archive({"m/a.onnx": b"evil"})))
    with pytest.raises(RuntimeError, match="checksum"):
        m._download(model_for(data))
    assert not list((tmp_path / "models").iterdir())  # nothing kept, nothing unpacked


def test_not_enough_disk_space_is_reported_before_downloading(tmp_path, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(models.shutil, "disk_usage", lambda p: Usage(10**9, 10**9, 50_000))
    server = FlakyServer(b"x")
    with pytest.raises(RuntimeError, match="Not enough free disk space"):
        manager(tmp_path, server)._download(model_for(b"x"))
    assert server.requests == []


def test_every_model_in_the_catalog_has_a_checksum():
    catalog = [models.VAD, models.SEGMENTATION, models.EMBEDDING, *models.ASR_MODELS.values()]
    assert all(len(m.sha256) == 64 for m in catalog)


def test_an_interrupted_unpack_never_counts_as_installed(tmp_path, monkeypatch):
    data = archive({"m/tokens.txt": b"t", "m/a.onnx": os.urandom(1000)})
    model = model_for(data, check="m/tokens.txt")
    m = manager(tmp_path, FlakyServer(data))
    real_extractall = tarfile.TarFile.extractall

    def killed_midway(self, path, *a, **k):
        (path / "m").mkdir(parents=True)
        (path / "m" / "tokens.txt").write_bytes(b"t")  # first file out, then the app is closed
        raise KeyboardInterrupt

    monkeypatch.setattr(tarfile.TarFile, "extractall", killed_midway)
    with pytest.raises(KeyboardInterrupt):
        m._download(model)
    assert not m.installed(model)
    monkeypatch.setattr(tarfile.TarFile, "extractall", real_extractall)
    m._download(model)  # next start: the finished download is reused, unpacked again, and only then installed
    assert m.installed(model) and (tmp_path / "models" / "m" / "a.onnx").exists()
    assert sorted(p.name for p in (tmp_path / "models").iterdir()) == ["m"]
