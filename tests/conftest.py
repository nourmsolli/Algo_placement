"""
conftest.py - Helpers pour importer le code des microservices dans les tests.

Chaque microservice est prévu pour tourner comme une app FastAPI indépendante,
avec son propre répertoire en tête de PYTHONPATH (voir run_local.sh). Comme
plusieurs services partagent des noms de fichiers (main.py, database.py),
on charge chaque module par son chemin de fichier sous un nom unique, pour
ne jamais se marcher dessus dans sys.modules pendant les tests.
"""

import sys
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_service_module(service_name: str, module_filename: str = "main.py"):
    """Charge services/<service_name>/<module_filename> sous un nom unique."""
    service_dir = SERVICES_DIR / service_name
    if str(service_dir) not in sys.path:
        sys.path.insert(0, str(service_dir))

    unique_name = f"{service_name}_{Path(module_filename).stem}"
    spec = importlib.util.spec_from_file_location(unique_name, service_dir / module_filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module
