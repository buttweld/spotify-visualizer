#!/usr/bin/env python3
"""Build the hardware-scaling helper locally for this machine's architecture."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--test", action="store_true", help="Build and run hardware-independent native tests")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
source = root / ("tests/native/test_kms_plane.c" if args.test else "native/kms_plane.c")
target = root / ".cache" / "native" / "libkms_plane.so"
target.parent.mkdir(parents=True, exist_ok=True)
compiler = shlex.split(os.environ.get("CC", "cc"))
cflags = shlex.split(os.environ.get("CPPFLAGS", ""))
ldflags = shlex.split(os.environ.get("LDFLAGS", ""))
fd, temporary = tempfile.mkstemp(prefix="kms-", suffix=".so", dir=target.parent)
os.close(fd)
testing = False
try:
    subprocess.run(compiler + ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                   "-I/usr/include/libdrm"] + ([] if args.test else ["-fPIC", "-shared"]) + cflags +
                   [str(source), "-o", temporary] + ldflags + ["-ldrm"], check=True)
    if args.test:
        testing = True
        os.chmod(temporary, 0o700)
        subprocess.run([temporary], check=True)
    else:
        os.replace(temporary, target)
except (OSError, subprocess.CalledProcessError):
    message = ("Native KMS tests failed; see the test output above." if testing else
               "Build failed. On the Pi install: sudo apt install build-essential libdrm-dev")
    print(message, file=sys.stderr)
    sys.exit(1)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
if not args.test:
    print(f"Built {target}. Rebuild on the Pi after syncing native source changes.")
