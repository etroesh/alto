"""Check that the cache budget really is read from the cgroup.

    python3 scripts/test_memory_budget.py

This exists because the first version of the cgroup reader looked plausible,
deployed cleanly, and silently returned the fallback on the live server - it
read the cgroup ROOT's limit, which is always "max", instead of the service's
own. Nothing failed; the number on /api/health was just quietly wrong.

So the parsing is checked against a fake cgroup directory laid out the way
systemd lays out a real one, rather than against whatever machine happens to
be running the test.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api import main


def check(name, got, expected):
    ok = got == expected
    print(("  PASS  " if ok else "  FAIL  ") + name)
    if not ok:
        print("          expected %r, got %r" % (expected, got))
    return ok


def main_test():
    passed = True

    # 1. The path is read out of /proc/self/cgroup, not assumed to be the root.
    #    A real systemd service reports: 0::/system.slice/alto-api.service
    fake_proc = tempfile.NamedTemporaryFile("w", suffix=".cgroup", delete=False)
    fake_proc.write("0::/system.slice/alto-api.service\n")
    fake_proc.close()

    import builtins
    original_open = builtins.open

    def patched_open(path, *args, **kwargs):
        if str(path) == "/proc/self/cgroup":
            return original_open(fake_proc.name, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    builtins.open = patched_open
    try:
        passed &= check("cgroup directory comes from /proc/self/cgroup",
                        main._own_cgroup_directory(),
                        "/sys/fs/cgroup/system.slice/alto-api.service")
    finally:
        builtins.open = original_open

    # 2. MemoryHigh=220M must produce a budget of 75% of it, and not the fallback.
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "memory.high").write_text(str(220 * 1024 * 1024) + "\n")
        original = main._own_cgroup_directory
        main._own_cgroup_directory = lambda: directory
        try:
            budget = round(main._memory_budget_mb(), 1)
            passed &= check("MemoryHigh=220M gives a 165 MB budget", budget, 165.0)
            passed &= check("the source names the real file",
                            main._memory_budget_source(), directory + "/memory.high")
        finally:
            main._own_cgroup_directory = original

    # 3. An unlimited cgroup must fall back, not crash or return zero.
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "memory.high").write_text("max\n")
        original = main._own_cgroup_directory
        main._own_cgroup_directory = lambda: directory
        try:
            passed &= check("an unlimited cgroup falls back",
                            main._memory_budget_mb(), main.FALLBACK_MEMORY_BUDGET_MB)
        finally:
            main._own_cgroup_directory = original

    # 4. The fallback must sit below what the unit file actually enforces.
    unit = Path(__file__).parent.parent / "deploy" / "alto-api.service"
    high = None
    for line in unit.read_text().splitlines():
        if line.startswith("MemoryHigh="):
            high = float(line.split("=")[1].rstrip("Mm"))
    passed &= check("the fallback is below the unit file's MemoryHigh (%s MB)" % high,
                    main.FALLBACK_MEMORY_BUDGET_MB < high, True)

    print("\n" + ("ALL PASSED" if passed else "SOMETHING FAILED"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main_test())
