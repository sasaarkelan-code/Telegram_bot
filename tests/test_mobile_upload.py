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
            modules = source / 'smm' if (source / 'smm').is_dir() else source
            if flattened:
                for path in modules.glob("*.py"):
                    if path.name != 'main.py':
                        shutil.copy(path, target / path.name)
            else:
                (target / 'smm').mkdir()
                for path in modules.glob('*.py'):
                    if path.name != 'main.py':
                        shutil.copy(path, target / 'smm' / path.name)
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
