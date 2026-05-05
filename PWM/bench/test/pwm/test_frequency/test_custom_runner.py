from __future__ import annotations

import sys
from pathlib import Path

from platformio.public import TestRunnerBase


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUNNERS_DIR = PROJECT_ROOT / "runners"
if str(RUNNERS_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNERS_DIR))

from common import run_script_suite


class CustomTestRunner(TestRunnerBase):
    def stage_building(self):
        return

    def stage_testing(self):
        run_script_suite(self, Path(__file__).with_name("bench_config.json"))
