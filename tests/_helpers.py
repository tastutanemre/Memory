"""Testler icin ortak yardimcilar: src yolu, izole MEM_HOME, gecici kasa."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MEM_PY = ROOT / "mem.py"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import mem  # noqa: E402

assert Path(mem.__file__).resolve().is_relative_to(SRC), "mem paketi src/ altindan yuklenmeli"

from mem import config, vault  # noqa: E402


class IsolatedCase(unittest.TestCase):
    """Her test: gecici dizin + MEM_HOME izolasyonu (gercek ev dizinine asla dokunmaz)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="memtest-")
        self.tmp = Path(self._tmp)
        self._saved_env = {k: os.environ.get(k) for k in ("MEM_HOME", "MEM_PROFILE")}
        os.environ["MEM_HOME"] = str(self.tmp / "home")
        os.environ.pop("MEM_PROFILE", None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self._tmp, ignore_errors=True)

    def make_vault(self, profile: str = "work", name: str = "vault", dlp_mode: str | None = None):
        root = self.tmp / name
        prof = config.create_profile(profile, root, dlp_mode=dlp_mode)
        vault.init_vault(root, profile, dlp_mode=prof.dlp_mode, state_dir=prof.state_dir)
        return root, Path(prof.state_dir)

    def make_repo(self, name: str = "repo") -> Path:
        p = self.tmp / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write_md(self, root: Path, rel: str, text: str) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
