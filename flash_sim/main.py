import sys
import threading
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# 运行配置：改这里即可；命令行直接执行：py main.py（在 flash_sim 目录下或设好 PYTHONPATH）
# - INPUT_JSON：传给 Start_simulation 的配置文件路径（建议写绝对路径）
# - MERGED_LOG：stdout 与 stderr 合并写入同一文件；None 表示仍输出到控制台。
# - MERGED_LOG_MIRROR_CONSOLE：为 True 时同时镜像到终端（等同 tee 的另一路 stdout）。
#   单文件句柄 + 互斥写入；每次 write 即 flush，顺序与「python -u … 2>&1 | tee file」一致。
# ---------------------------------------------------------------------------
_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parent
INPUT_JSON = str(_REPO_ROOT / "test_case" / "test_read_write.json")
MERGED_LOG = str(_BASE / "output" / "test_read_write.log")
MERGED_LOG_MIRROR_CONSOLE = True


def _validate_input_paths() -> None:
    missing = []
    for path_str in (INPUT_JSON,):
        if path_str and not Path(path_str).exists():
            missing.append(path_str)
    if missing:
        raise FileNotFoundError("Missing input file(s): " + ", ".join(missing))


class _LockedMergedStream:
    """合并写文件；可选镜像到原 stdout（stderr 经本对象写入时也在终端同一流显示，同 2>&1 | tee）。"""

    __slots__ = ("_f", "_lock", "_console")

    def __init__(self, f, console=None):
        self._f = f
        self._lock = threading.Lock()
        self._console = console

    def write(self, s):
        if not s:
            return 0
        with self._lock:
            n = self._f.write(s)
            self._f.flush()
            if self._console is not None:
                self._console.write(s)
                self._console.flush()
            return n

    def flush(self):
        with self._lock:
            self._f.flush()
            if self._console is not None:
                self._console.flush()

    def __getattr__(self, name):
        return getattr(self._f, name)


if __package__ in (None, ""):
    import os
    import sys

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from flash_sim.engine import Engine
    from flash_sim.common import format_event_queue
else:
    from .engine import Engine
    from .common import format_event_queue


def print_tsu_chip_queues(engine):
    """在 finally 中调用：仅打印 TSU 的 chip_queue（queues[channel][chip][type]）。"""
    sep = "========================================"
    print(f"\n{sep}")
    print("TSU chip_queue (queues[channel][chip][type])")
    print(sep)
    try:
        tsu = engine.device.ftl.tsu
        for ch in range(len(tsu.queues)):
            for chip in range(len(tsu.queues[ch])):
                by_type = tsu.queues[ch][chip]
                counts = {k: len(by_type[k]) for k in by_type if by_type[k]}
                if not counts:
                    continue
                print(f"  (ch={ch}, chip={chip}): {counts}")
                for tt, lst in by_type.items():
                    if lst:
                        for i, trans in enumerate(lst[:2]):
                            print(f"    {tt}[{i}]: {trans}")
                        if len(lst) > 2:
                            print(f"    ... and {len(lst) - 2} more")
    except Exception as e:
        print(f"  (TSU dump error: {e})")
    print(f"\n{sep}\n")


if __name__ == "__main__":
    _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
    _merged_backing = None
    try:
        if MERGED_LOG:
            Path(MERGED_LOG).parent.mkdir(parents=True, exist_ok=True)
            _merged_backing = open(
                MERGED_LOG, "w", encoding="utf-8", newline="\n", buffering=1
            )
            _mirror = (
                _orig_stdout if MERGED_LOG_MIRROR_CONSOLE else None
            )
            _merged = _LockedMergedStream(_merged_backing, _mirror)
            sys.stdout = _merged
            sys.stderr = _merged
        else:
            stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
            stderr_reconfigure = getattr(sys.stderr, "reconfigure", None)
            if callable(stdout_reconfigure):
                stdout_reconfigure(line_buffering=True)
            if callable(stderr_reconfigure):
                stderr_reconfigure(line_buffering=True)

        sim_engine = Engine()
        print("Module construction complete.\n\n")
        try:
            _validate_input_paths()
            sim_engine.Start_simulation(INPUT_JSON)
        except Exception as e:
            print(f"Error: {e}")
            try:
                print(
                    "address_mapping_unit.gtd:",
                    sim_engine.device.ftl.address_mapping_unit.gtd,
                )
            except Exception as _:
                print("(address_mapping_unit.gtd not available:", _)
            print("\n--- Traceback (most recent call last) ---")
            traceback.print_exc()
        finally:
            print("Simulation completed.")
            print(f"Simulation time: {sim_engine.Get_current_time()}")
            print(format_event_queue(sim_engine.event_queue.queue))
            print(sim_engine.device.ftl.address_mapping_unit.cmt.cache)
            print(sim_engine.device.ftl.address_mapping_unit.gmt)
            print(sim_engine.device.ftl.address_mapping_unit.gtd)
    finally:
        if _merged_backing is not None:
            try:
                sys.stdout.flush()
            except Exception:
                pass
            try:
                sys.stderr.flush()
            except Exception:
                pass
            _merged_backing.flush()
            _merged_backing.close()
            sys.stdout = _orig_stdout
            sys.stderr = _orig_stderr
