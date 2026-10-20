"""Keep real AstrBot imports isolated from the user's runtime configuration."""

import os
import tempfile

# AstrBot creates config and database files during import.
_runtime = tempfile.TemporaryDirectory(prefix="jipiao-tests-")
os.environ["ASTRBOT_ROOT"] = _runtime.name
os.environ["TEST_MODE"] = "on"
