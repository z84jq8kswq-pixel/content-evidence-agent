"""密钥文件的单元测试：密钥要能放在项目树外。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evidence_agent import envfile  # noqa: E402

KEY = "EVIDENCE_AGENT_TEST_KEY"
NL = chr(10)


class EnvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.outside = self.tmp / "outside" / "evidence-agent.env"
        self.outside.parent.mkdir(parents=True)

        self._saved_paths = envfile.EXTERNAL_ENV_PATHS
        envfile.EXTERNAL_ENV_PATHS = (self.outside,)
        self._saved_key = os.environ.pop(KEY, None)

    def tearDown(self) -> None:
        envfile.EXTERNAL_ENV_PATHS = self._saved_paths
        if self._saved_key is None:
            os.environ.pop(KEY, None)
        else:
            os.environ[KEY] = self._saved_key

    def test_loads_from_project_file(self) -> None:
        (self.project / ".env.local").write_text(KEY + "=project" + NL, encoding="utf-8")
        self.assertEqual(envfile.load_env_files(self.project), [KEY])
        self.assertEqual(os.environ[KEY], "project")

    def test_loads_from_outside_the_project_tree(self) -> None:
        self.outside.write_text(KEY + "=outside" + NL, encoding="utf-8")
        self.assertEqual(envfile.load_env_files(self.project), [KEY])
        self.assertEqual(os.environ[KEY], "outside")

    def test_project_file_wins_over_the_external_one(self) -> None:
        (self.project / ".env.local").write_text(KEY + "=project" + NL, encoding="utf-8")
        self.outside.write_text(KEY + "=outside" + NL, encoding="utf-8")

        envfile.load_env_files(self.project)
        self.assertEqual(os.environ[KEY], "project")

    def test_existing_environment_variable_wins(self) -> None:
        os.environ[KEY] = "from-shell"
        self.outside.write_text(KEY + "=outside" + NL, encoding="utf-8")

        self.assertEqual(envfile.load_env_files(self.project), [])
        self.assertEqual(os.environ[KEY], "from-shell")

    def test_missing_files_are_not_an_error(self) -> None:
        self.assertEqual(envfile.load_env_files(self.project), [])


if __name__ == "__main__":
    unittest.main()
