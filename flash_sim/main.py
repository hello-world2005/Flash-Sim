import sys
import traceback

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
    # 禁止缓冲，使 print 立即输出（便于日志/重定向时实时查看）
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    sim_engine = Engine()
    print("Module construction complete.\n\n")
    try:
        sim_engine.Start_simulation(r"E:\Files\Li_Meng\HBF\Flash-Sim\examples\static_write_test.json")
    except Exception as e:
        print(f"Error: {e}")
        try:
            print("address_mapping_unit.gtd:", sim_engine.device.ftl.address_mapping_unit.gtd)
        except Exception as _:
            print("(address_mapping_unit.gtd not available:", _)
        print("\n--- Traceback (most recent call last) ---")
        traceback.print_exc()
    finally:
        print("Simulation completed.")
        print(f"Simulation time: {sim_engine.Get_current_time()}")
        print(format_event_queue(sim_engine.event_queue.queue))
        # print_tsu_chip_queues(sim_engine)
        # print("\n\naddress_mapping_unit.gtd:")
        # print(sim_engine.device.ftl.address_mapping_unit.gtd)
