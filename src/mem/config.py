"""Profil (work / personal / ozel) ve state dizini yonetimi, izinli repo beyaz listesi."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .lock import atomic_write_text, read_text_retry
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
DLP_MODES = ("block", "redact", "off")
VAULT_SCOPE = "_vault"


class ProfileError(ValueError):
    """Profil bulunamadi veya gecersiz."""


@dataclass
class Profile:
    name: str
    vault_root: str
    state_dir: str
    dlp_mode: str

    def to_dict(self) -> dict:
        return {
            "vault_root": self.vault_root,
            "state_dir": self.state_dir,
            "dlp_mode": self.dlp_mode,
        }


# ---------------------------------------------------------------- yollar


def mem_home() -> Path:
    env = os.environ.get("MEM_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "mem-engine"
    return Path.home() / ".local" / "share" / "mem-engine"


def profiles_path() -> Path:
    return mem_home() / "profiles.json"


def default_state_dir(profile_name: str) -> Path:
    return mem_home() / "state" / profile_name


def default_vault_dir(profile_name: str) -> Path:
    if os.environ.get("MEM_HOME"):
        return mem_home() / "vaults" / profile_name
    return Path.home() / "MemVault" / profile_name


def norm_path(p) -> str:
    """Karsilastirma icin normalize yol: realpath, `/` ayiracli, kucuk harf."""
    s = os.path.realpath(os.path.abspath(str(p)))
    s = s.replace("\\", "/")
    if len(s) > 1 and s.endswith("/") and not re.match(r"^[A-Za-z]:/$", s):
        s = s.rstrip("/")
    return s.lower()


def is_within(child, parent) -> bool:
    c = norm_path(child)
    p = norm_path(parent)
    if c == p:
        return True
    if not p.endswith("/"):
        p += "/"
    return c.startswith(p)


def ensure_state_outside(vault_root, state_dir) -> None:
    """state_dir ile vault_root kesisemez (iki yonlu)."""
    if is_within(state_dir, vault_root) or is_within(vault_root, state_dir):
        raise ValueError(
            "state_dir kasa (vault_root) ile kesisemez: indeks ve kilit dosyalari kasanin disinda olmali."
        )


def default_dlp_mode(profile_name: str) -> str:
    return "block" if profile_name == "work" else "redact"


# ---------------------------------------------------------------- profiller


def _load_registry() -> dict:
    path = profiles_path()
    if not path.exists():
        return {"schema": 1, "default": None, "profiles": {}}
    try:
        data = json.loads(read_text_retry(path))
    except (OSError, ValueError) as exc:
        raise ProfileError(f"profiles.json okunamadi: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError("profiles.json gecersiz")
    data.setdefault("profiles", {})
    data.setdefault("default", None)
    return data


def _save_registry(data: dict) -> None:
    atomic_write_text(profiles_path(), json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def list_profiles() -> list[Profile]:
    data = _load_registry()
    out = []
    for name, p in data["profiles"].items():
        out.append(
            Profile(
                name=name,
                vault_root=p["vault_root"],
                state_dir=p.get("state_dir") or str(default_state_dir(name)),
                dlp_mode=p.get("dlp_mode") or default_dlp_mode(name),
            )
        )
    return out


def create_profile(name: str, vault_root, state_dir=None, dlp_mode: str | None = None) -> Profile:
    if not PROFILE_NAME_RE.match(name or ""):
        raise ProfileError(f"Gecersiz profil adi: {name!r}")
    mode = dlp_mode or default_dlp_mode(name)
    if mode not in DLP_MODES:
        raise ValueError(f"Gecersiz dlp_mode: {mode!r}")
    vroot = os.path.abspath(str(vault_root))
    sdir = os.path.abspath(str(state_dir)) if state_dir else str(default_state_dir(name))
    ensure_state_outside(vroot, sdir)
    data = _load_registry()
    data["profiles"][name] = {"vault_root": vroot, "state_dir": sdir, "dlp_mode": mode}
    if not data.get("default"):
        data["default"] = name
    _save_registry(data)
    return Profile(name, vroot, sdir, mode)


def get_profile(name: str | None = None) -> Profile:
    data = _load_registry()
    profiles = data["profiles"]
    wanted = name or os.environ.get("MEM_PROFILE") or data.get("default")
    if not wanted:
        if len(profiles) == 1:
            wanted = next(iter(profiles))
        else:
            raise ProfileError("Profil secilmedi ve kayitli varsayilan profil yok. `mem init` calistirin.")
    if wanted not in profiles:
        raise ProfileError(f"Profil bulunamadi: {wanted!r}. Kayitli profiller: {sorted(profiles)}")
    p = profiles[wanted]
    state = p.get("state_dir") or str(default_state_dir(wanted))
    ensure_state_outside(p["vault_root"], state)
    return Profile(wanted, p["vault_root"], state, p.get("dlp_mode") or default_dlp_mode(wanted))


def profile_for_vault(vault_root) -> Profile | None:
    for p in list_profiles():
        if norm_path(p.vault_root) == norm_path(vault_root):
            return p
    return None


# ---------------------------------------------------------------- kasa meta


def vault_meta_path(vault_root) -> Path:
    return Path(vault_root) / ".mem-vault.json"


def read_vault_meta(vault_root) -> dict:
    path = vault_meta_path(vault_root)
    if not path.exists():
        raise ValueError(f"Kasa baslatilmamis (.mem-vault.json yok): {vault_root}")
    try:
        return json.loads(read_text_retry(path))
    except (OSError, ValueError) as exc:
        raise ValueError(f".mem-vault.json okunamadi: {exc}") from exc


def new_vault_meta(profile_name: str, dlp_mode: str) -> dict:
    return {
        "schema": 1,
        "profile": profile_name,
        "dlp_mode": dlp_mode,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def resolve_state_dir(vault_root, state_dir=None) -> Path:
    """Acik state_dir > profiles.json eslesmesi > MEM_HOME/state/<profil>."""
    if state_dir:
        sdir = Path(state_dir)
    else:
        prof = profile_for_vault(vault_root)
        if prof is not None:
            sdir = Path(prof.state_dir)
        else:
            try:
                name = read_vault_meta(vault_root).get("profile") or "default"
            except ValueError:
                name = "default"
            sdir = default_state_dir(name)
    ensure_state_outside(vault_root, sdir)
    return sdir


# ---------------------------------------------------------------- projeler


def projects_file(vault_root) -> Path:
    return Path(vault_root) / "projects.json"


def load_projects(vault_root) -> list[dict]:
    path = projects_file(vault_root)
    if not path.exists():
        return []
    try:
        data = json.loads(read_text_retry(path))
    except (OSError, ValueError):
        return []
    projects = data.get("projects", []) if isinstance(data, dict) else []
    return [p for p in projects if isinstance(p, dict) and p.get("slug")]


def save_projects(vault_root, projects: list[dict]) -> None:
    atomic_write_text(
        projects_file(vault_root),
        json.dumps({"projects": projects}, ensure_ascii=False, indent=2) + "\n",
    )


def get_project(vault_root, slug: str) -> dict | None:
    for p in load_projects(vault_root):
        if p["slug"] == slug:
            return p
    return None


def resolve_project(vault_root, cwd) -> dict | None:
    """cwd izinli roots icindeyse projeyi, kasa icindeyse `_vault` kapsamini, yoksa None (en uzun kok kazanir)."""
    if not cwd:
        return None
    best = None
    best_len = -1
    if is_within(cwd, vault_root):
        best = {"slug": VAULT_SCOPE, "name": VAULT_SCOPE, "roots": [str(vault_root)], "description": ""}
        best_len = len(norm_path(vault_root))
    for proj in load_projects(vault_root):
        for root in proj.get("roots", []):
            if is_within(cwd, root):
                n = len(norm_path(root))
                if n > best_len:
                    best, best_len = proj, n
    return best
