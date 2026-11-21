"""批量冒烟 v2：每个 (示例 × 图类型) 在独立子进程渲染，90s 看门狗防挂死。

用法: python smoke_test.py            # 全量
"""
import os
import sys
import multiprocessing as mp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CASE_TIMEOUT = 90  # 秒


def worker(case, conn):
    sid, pid = case
    try:
        import samples as S
        import data_profiler as dp
        import plot_registry as reg
        df = S.SAMPLES[sid][2]()
        prof = dp.profile_df(df)
        pdef = reg.PLOTS[pid]
        P = reg.autofill(prof, pid, {})
        missing = [s["key"] for s in pdef["params"] if s.get("required") and not P.get(s["key"])]
        if missing:
            conn.send(("SKIP", f"{sid}×{pid} (缺必填: {','.join(missing)})"))
            return
        png, html, ins, warns, _ = reg.render_plot(df, prof, pid, P, embed_js=False)
        if not png and not html:
            conn.send(("FAIL", f"{sid}×{pid} (双引擎空产物)"))
        elif png and len(png) < 2500:
            conn.send(("FAIL", f"{sid}×{pid} (PNG 仅 {len(png)}B 疑似空图)"))
        else:
            size = f"{len(png)//1024}KB" if png else "仅HTML"
            conn.send(("OK", f"{sid}×{pid} ({size})"))
    except ValueError as e:
        conn.send(("SKIP", f"{sid}×{pid} (域保护: {e})"))
    except Exception as e:
        import traceback
        conn.send(("FAIL", f"{sid}×{pid} ({type(e).__name__}: {e})"))
        traceback.print_exc()


if __name__ == "__main__":
    mp.set_start_method("fork", force=True)
    import samples as S
    import plot_registry as reg

    cases = [(sid, pid) for sid in S.SAMPLES for pid in reg.PLOTS]
    print(f"共 {len(cases)} 个用例，看门狗 {CASE_TIMEOUT}s", flush=True)
    ok, skip, fail, hang = [], [], [], []
    for i, case in enumerate(cases):
        parent, child = mp.Pipe()
        p = mp.Process(target=worker, args=(case, child))
        p.start()
        child.close()
        p.join(CASE_TIMEOUT)
        if p.is_alive():
            p.terminate()
            p.join(5)
            hang.append(f"{case[0]}×{case[1]}")
            print(f"[{i+1}/{len(cases)}] HANG {case[0]}×{case[1]}", flush=True)
            continue
        status, msg = parent.recv() if parent.poll(1) else ("FAIL", f"{case[0]}×{case[1]} (无返回)")
        {"OK": ok, "SKIP": skip, "FAIL": fail}[status].append(msg)
        flag = "✓" if status == "OK" else ("·" if status == "SKIP" else "✗")
        print(f"[{i+1}/{len(cases)}] {flag} {msg}", flush=True)
        parent.close()

    print(f"\n===== 冒烟结果: {len(ok)} OK / {len(skip)} SKIP / {len(fail)} FAIL / {len(hang)} HANG =====", flush=True)
    for f in fail:
        print("FAIL:", f, flush=True)
    for h in hang:
        print("HANG:", h, flush=True)
