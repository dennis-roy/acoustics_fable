"""Prove that the host C test catches a deliberately weakened count bound."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / "src/la_core.c").read_text()
original = "if (count > LA_COUNT_MAX) return false;"
assert source.count(original) == 1
with tempfile.TemporaryDirectory(prefix="la-count-mutation-") as directory:
    path = Path(directory)
    mutated = path / "la_core.c"
    mutated.write_text(source.replace(original, "if (count > LA_COUNT_MAX + 1u) return false;"))
    executable = path / "la_core_test"
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I" + str(root / "src"),
                    str(root / "host/la_core_test.c"), str(mutated), "-lm", "-o", str(executable)], check=True)
    result = subprocess.run([str(executable)], capture_output=True, text=True)
    print(result.stdout)
    assert result.returncode == 1 and "FAIL line" in result.stdout, "mutation was not detected by an assertion"
    print("PASS: deliberately weakened count bound detected by host test")
