import gc
import os
import shutil
import stat
import sys
import tempfile
import traceback
import unittest


def remove_tree(root):
    def retry(func, path, _):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    gc.collect()
    shutil.rmtree(root, onerror=retry)
    if os.path.exists(root):
        print("could not remove every temp file (still open?): %s" % root)


def run(namespace, argv=None):
    argv = sys.argv[1:] if argv is None else argv
    tests = [(n, f) for n, f in sorted(namespace.items()) if n.startswith("test_") and callable(f)]
    unknown = [a for a in argv if a not in dict(tests)]
    if unknown:
        print("unknown test(s): %s" % ", ".join(unknown))
        return 2
    if argv:
        tests = [(n, f) for n, f in tests if n in argv]
    failed = skipped = 0
    root = tempfile.mkdtemp(prefix="adtok-tests-")
    previous, tempfile.tempdir = tempfile.tempdir, root
    try:
        for name, fn in tests:
            try:
                fn()
                print("PASS", name)
            except unittest.SkipTest as e:
                skipped += 1
                print("SKIP", name, "-", e)
            except Exception:
                failed += 1
                print("FAIL", name)
                traceback.print_exc()
    finally:
        tempfile.tempdir = previous
        if failed:
            print("kept temp files for debugging: %s" % root)
        else:
            remove_tree(root)
    print("%d of %d passed%s" % (len(tests) - failed - skipped, len(tests), ", %d skipped" % skipped if skipped else ""))
    return 1 if failed else 0
