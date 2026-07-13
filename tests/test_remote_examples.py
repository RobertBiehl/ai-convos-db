import subprocess, sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario",("personal","team"))
def test_remote_usage_example(scenario):
    script=Path(__file__).parents[1]/"examples/remote/demo.py"; result=subprocess.run((sys.executable,str(script),scenario),text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr; assert f'"scenario": "{scenario}"' in result.stdout and '"relay_plaintext": false' in result.stdout
