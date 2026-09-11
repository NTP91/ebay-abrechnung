import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import atomic_io


class AtomicReplaceTests(unittest.TestCase):
    def test_retries_transient_permission_error_and_replaces(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/"state.tmp";destination=Path(folder)/"state.json"
            source.write_text("new",encoding="utf-8");destination.write_text("old",encoding="utf-8")
            real=atomic_io.os.replace; calls=[]
            def flaky(src,dst):
                calls.append((src,dst))
                if len(calls)<3: raise PermissionError(5,"temporarily locked")
                return real(src,dst)
            with patch.object(atomic_io.os,"replace",side_effect=flaky),patch.object(atomic_io.time,"sleep"):
                atomic_io.replace_file(source,destination)
            self.assertEqual(destination.read_text(encoding="utf-8"),"new")
            self.assertFalse(source.exists())
            self.assertEqual(len(calls),3)

    def test_raises_after_bounded_attempts(self):
        error=PermissionError(5,"locked")
        with patch.object(atomic_io.os,"replace",side_effect=error),patch.object(atomic_io.time,"sleep"):
            with self.assertRaises(PermissionError):
                atomic_io.replace_file("source","destination",attempts=3,base_delay=0)


if __name__ == "__main__":
    unittest.main()
