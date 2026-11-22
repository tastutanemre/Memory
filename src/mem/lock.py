"""Coklu ajan dosya kilidi ve atomik yazma; kasa disina cikis (path traversal) korumasi."""

from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

import hashlib

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - POSIX
    import fcntl


class PathTraversalError(ValueError):
    """Yol kasa disina cikmaya calisiyor."""


class ConcurrencyConflictError(RuntimeError):
    """Dosya beklenen sha256'dan farkli (baska bir ajan degistirmis)."""

    def __init__(self, message: str, current_sha256: str | None = None):
        super().__init__(message)
        self.current_sha256 = current_sha256


class VaultLock:
    """OS dosya kilidi (msvcrt / fcntl) context manager. Ayni surec icinde yeniden girilemez."""

    def __init__(self, path, timeout: float = 15.0, poll: float = 0.05):
        self.path = Path(path)
        self.timeout = timeout
        self.poll = poll
        self._fh = None

    def _try_lock(self) -> None:
        self._fh.seek(0)
        if os.name == "nt":
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._try_lock()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(f"Kasa kilidi {self.timeout:.0f}s icinde alinamadi: {self.path}")
                time.sleep(self.poll)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_in_vault(vault_root, rel) -> Path:
    """Goreli yolu kasa icinde cozer; `..`, mutlak yol ve disari giden symlink'te PathTraversalError."""
    rel_s = str(rel).replace("\\", "/")
    if not rel_s or "\x00" in rel_s:
        raise PathTraversalError("Bos veya gecersiz yol")
    if rel_s.startswith("/") or re.match(r"^[A-Za-z]:", rel_s):
        raise PathTraversalError("Mutlak yol kabul edilmez")
    parts = [p for p in rel_s.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise PathTraversalError("Kasa disina cikis denemesi")
    root = Path(os.path.realpath(vault_root))
    candidate = Path(os.path.abspath(vault_root)).joinpath(*parts)
    real = Path(os.path.realpath(candidate))
    if real != root and root not in real.parents:
        raise PathTraversalError("Yol kasa disina cikiyor")
    return candidate


def _win_ext_path(p: Path) -> str:
    """Windows yoluna kosulsuz `\\\\?\\` (veya `\\\\?\\UNC\\`) uzun yol oneki ekler."""
    s = os.path.abspath(str(p))
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def _fs_path(p: Path) -> str | Path:
    """Windows MAX_PATH (>=240 UTF-16 kod birimi) sinirina yaklasan yollara `\\\\?\\` oneki ekler."""
    if os.name == "nt":
        s = os.path.abspath(str(p))
        if not s.startswith("\\\\?\\"):
            units = sum(2 if ord(c) > 0xFFFF else 1 for c in s)
            if units >= 240:
                return _win_ext_path(p)
    return p


def _backup_prefix(name: str) -> str:
    """Kisa adlari korur, 48 karakterden uzun dosya adlarini hash ile kisaltir."""
    if len(name) <= 48:
        return name
    p = Path(name)
    suf = p.suffix[:10]
    stem = p.stem[:36]
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}{suf}"


def _prune_backups(backup_dir: Path, name: str, keep: int = 20) -> None:
    files = sorted(Path(_fs_path(backup_dir)).glob(name + ".*.bak"), key=lambda p: p.name)
    for old in files[:-keep]:
        try:
            os.unlink(_fs_path(old))
        except OSError:
            pass


def atomic_write_bytes(path, data: bytes, backup_dir=None) -> str:
    """Ham baytlari yedekli + tmp + fsync + os.replace ile yazar; yeni sha256 dondurur.

    Mevcut hedef dosyanin POSIX izin bitleri (st_mode & 0o777) korunur; yeni olusturulan
    dosyalarda standart Git/Markdown uyumu icin surecin aktif umask politikasi uygulanir.
    """
    path = Path(path)
    Path(_fs_path(path.parent)).mkdir(parents=True, exist_ok=True)
    path_fs = _fs_path(path)
    prev_mode: int | None = None
    if os.path.exists(path_fs):
        if os.name != "nt":
            try:
                prev_mode = os.stat(path_fs).st_mode & 0o777
            except OSError:
                prev_mode = None
        if backup_dir is not None:
            backup_dir = Path(backup_dir)
            Path(_fs_path(backup_dir)).mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
            bname = _backup_prefix(path.name)
            shutil.copy2(path_fs, _fs_path(backup_dir / f"{bname}.{stamp}.bak"))
            _prune_backups(backup_dir, bname)
    tmp = path.with_name(f".mem-tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp_fs = _fs_path(tmp)
    try:
        with open(tmp_fs, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt" and prev_mode is not None:
            os.chmod(tmp_fs, prev_mode)
        delay = 0.0125
        for attempt in range(8):
            try:
                os.replace(tmp_fs, path_fs)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)
    finally:
        if os.path.exists(tmp_fs):
            try:
                os.unlink(tmp_fs)
            except OSError:
                pass
    return sha256_bytes(data)


def atomic_write_text(path, content: str, backup_dir=None) -> str:
    """UTF-8, `\\n` satir sonu; tmp + fsync + os.replace. Yeni sha256 dondurur."""
    return atomic_write_bytes(path, content.replace("\r\n", "\n").encode("utf-8"), backup_dir=backup_dir)


def read_text_retry(path, attempts: int = 10) -> str:
    """Windows'ta eszamanli os.replace sirasinda gecici PermissionError'a karsi kisa geri cekilmeli okuma."""
    delay = 0.005
    for i in range(attempts):
        try:
            return Path(_fs_path(Path(path))).read_text(encoding="utf-8")
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)
    raise AssertionError("unreachable")
