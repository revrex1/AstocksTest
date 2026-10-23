"""统一入口：python run.py open|close，带日志落盘。

供 Windows 计划任务调用；参数透传给 astocks.cli。
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from astocks import cli  # noqa: E402


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    log_dir = os.path.join(ROOT, "data", "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{datetime.now():%Y-%m-%d}.log")

    class Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, s):
            for st in self.streams:
                try:
                    st.write(s)
                    st.flush()
                except Exception:
                    pass

        def flush(self):
            for st in self.streams:
                try:
                    st.flush()
                except Exception:
                    pass

    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {mode} =====\n")
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = Tee(old_out, f)
        sys.stderr = Tee(old_err, f)
        try:
            return cli.main([mode] + sys.argv[2:])
        except Exception:
            traceback.print_exc()
            return 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err


if __name__ == "__main__":
    raise SystemExit(main())
