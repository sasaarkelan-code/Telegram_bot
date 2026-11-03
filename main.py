"""Start with either the original smm directory or a flattened mobile upload."""
import importlib.util
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
if not (root / "smm").is_dir():
    required = ("__init__.py", "hosting.py", "config.py", "store.py")
    if not all((root / name).is_file() for name in required):
        raise RuntimeError("Не хватает файлов бота. Загрузи всё содержимое архива.")
    spec = importlib.util.spec_from_file_location(
        "smm", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules["smm"] = package
    spec.loader.exec_module(package)

from smm.hosting import main

if __name__ == "__main__":
    raise SystemExit(main())
