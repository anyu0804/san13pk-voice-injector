"""全表 4015 槽 small 模型 ASR（填充 data/asr-cache 缓存）。用法：scan_all.py [start] [end]。

无参数=全表 0..4015；给区间则只扫该段（已缓存的槽自动跳过），可多进程并行分片。
"""

import sys
import time

sys.path.insert(0, ".")
import voice_inject as V

lo = int(sys.argv[1]) if len(sys.argv) > 1 else 0
hi = int(sys.argv[2]) if len(sys.argv) > 2 else 4015
t0 = time.time()
done = 0
for s in range(lo, min(hi, 4015)):
    V.asr_slot_text(s)
    done += 1
    if done % 50 == 0:
        rate = done / (time.time() - t0)
        eta = (min(hi, 4015) - s - 1) / max(rate, 0.01) / 60
        print(f"[{time.strftime('%H:%M:%S')}] {s+1}/{hi}  {rate:.2f}s/槽  ETA {eta:.0f}min", flush=True)
print(f"RANGE {lo}-{hi} DONE", flush=True)
