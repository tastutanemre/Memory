"""Sifir-pip giris noktasi: `python mem.py <komut>`.

`src` dizinini sys.path basina ekler ve dogrudan mem.cli.main() cagirir.
Kok dizinde `import mem` / `python -m mem` bu dosyayi paketin onune gecirmesin diye
`src` her kosulda sys.path[0]'a alinir; modul olarak import edilirse gercek paket yuklenir.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if _SRC in sys.path:
    sys.path.remove(_SRC)
sys.path.insert(0, _SRC)

if __name__ != "__main__":
    # `import mem` kok dizindeki bu dosyayi buldu (golgeleme): gercek paketi sys.modules["mem"]'e yukle.
    import importlib.util

    _pkg = os.path.join(_SRC, "mem")
    _spec = importlib.util.spec_from_file_location(
        "mem", os.path.join(_pkg, "__init__.py"), submodule_search_locations=[_pkg]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["mem"] = _module
    _spec.loader.exec_module(_module)
else:
    from mem.cli import main

    sys.exit(main())
