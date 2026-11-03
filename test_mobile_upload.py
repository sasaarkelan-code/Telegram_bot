import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class MobileUploadTests(unittest.TestCase):
    def launch(self, flattened):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            shutil.copy(source / "main.py", target / "main.py")
            if flattened:
                for path in (source / "smm").glob("*.py"):
                    shutil.copy(path, target / path.name)
            else:
                shutil.copytree(source / "smm", target / "smm", ignore=shutil.ignore_patterns("__pycache__"))
            env = dict(os.environ, SMM_DATA_DIR=str(target / "data"))
            env.pop("BOT_TOKEN", None)
            env.pop("OWNER_ID", None)
            result = subprocess.run([sys.executable, "main.py"], cwd=target, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("BOT_TOKEN", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_original_folder_structure_starts(self):
        self.launch(False)

    def test_flattened_mobile_upload_starts(self):
        self.launch(True)
