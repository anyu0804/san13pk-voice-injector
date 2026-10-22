"""San13PK 专属语音替换流水线（san13pk-voice-injector）。

命令：
  list                         列出可用武将/语音包/bank 定位状态
  locate  <武将>               ASR 扫描定位该武将专属语音库槽区（缓存）
  plan    <语音包目录> <武将>  槽位分桶 + 包内词级ASR + 裁切分配 → plan.json + 报告
  build   <语音包目录> <武将>  按 plan 编码打包 KOVS、备份原文、门禁1-3（不写包）
  write   <语音包目录> <武将>  门禁3b/3c + 写入 + 门禁4 全验证
  rollback <武将> <运行目录>   从 originals 逐槽还原

四道门禁（全部 PASS 才落盘/才算成功）：
  门禁1 独占性：目标 Voice 库在 855 武将表中引用数=1；目标槽区无同偏移别名
  门禁2 定位：bank-map verified（≥2 个自称锚字命中 + 边界外侧非本库）
  门禁3 容量与基底：每槽 payload ≤ 物理间隙；游戏未运行；写前断言当前字节==备份
  门禁4 写后验证：逐槽读回哈希 + 文件内解码 + 4MB 块哈希受限差异 + 帧表未动 + 大小不变

铁律：只动已定位专属槽区；帧表(0x894B8600)永不动；只写 pk\\DATA1.S13；根目录包不碰；
      ffmpeg 经 stdout 管道写盘（AV 拦截旁路）；KOVS 头 size@4=流长、flag@0x10=1。
"""

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

# ---- 路径配置（全部可用环境变量覆盖，见 README“环境变量”一节）----
SKILL = Path(__file__).resolve().parent.parent
DATA = SKILL / "data"
ROOT = Path(os.environ.get("SAN13PK_HOME", r"D:\San13PK"))
PK = ROOT / "pk" / "DATA1.S13"
EXPORT = Path(os.environ.get(
    "SAN13PK_EXPORT",
    str(ROOT / "outputs" / "voice-bank" / "shared-scenario-export.s13")))
BASE = 0x894B8600
_ff_env = os.environ.get("VJ_FFMPEG_DIR")
if _ff_env and (Path(_ff_env) / "ffmpeg.exe").exists():
    FFDIR = Path(_ff_env)
elif (SKILL / "bin" / "ffmpeg.exe").exists():
    FFDIR = SKILL / "bin"
else:
    FFDIR = None  # 回退到 PATH 中的 ffmpeg / ffprobe
FF = str(FFDIR / "ffmpeg.exe") if FFDIR else "ffmpeg"
FFPROBE = str(FFDIR / "ffprobe.exe") if FFDIR else "ffprobe"
ASR_HOME = Path(os.environ.get("SAN13PK_ASR_HOME", str(ROOT / "outputs" / "asr")))
OUTROOT = Path(os.environ.get("SAN13PK_OUT", str(ROOT / "outputs" / "inject")))
SCENE_FILES = {
    1: "01_初次拜访_greet.wav", 2: "02_日常寒暄_chat.wav", 3: "03_接受赠礼_gift_accept.wav",
    4: "04_婉拒拉拢_gift_reject.wav", 5: "05_答应出仕_join.wav", 6: "06_舌战辩驳_debate.wav",
    7: "07_战场出阵_battle_order.wav", 8: "08_战法暴击_critical.wav", 9: "09_生死羁绊_bond_vow.wav",
    10: "10_通用短语气与战斗口令.wav", 11: "11_日常答复与舌战交锋.wav",
}
OTHER_NAMES = ["子龙", "赵云", "周瑜", "公瑾", "刘备", "玄德", "曹操", "孟德", "孔明", "诸葛亮",
               "司马懿", "仲达", "关羽", "云长", "张飞", "翼德", "吕布", "奉先", "孙权", "仲谋",
               "貂蝉", "月英", "姜维", "伯约", "魏延", "文长", "马超", "孟起", "黄忠", "汉升",
               "孙众谋", "曹孟德", "刘玄德", "关云长", "张翼德", "吕奉先", "马孟起", "黄汉升"]
SYSTEM_PHRASES = ["击败敌将", "敌将逼近", "大本营", "阵营被敌军", "敌军正向", "援军已经",
                  "口信", "他国的使者", "有人通敌", "辞官", "我收到了", "有客人来了",
                  "俘虏敌将", "战胜了", "败走了",
                  "擊敗敵將", "俘虜敵將", "戰勝了", "敗走了"]
SYSTEM_EXACT = {"报告", "明白", "是", "罪", "罪名", "出发"}
REGION_BATTLE = [(1500, 1800), (1800, 2060), (840, 1500)]
REGION_DIALOG = [(3700, 4015), (2060, 3700)]
STRIDE = 3
BYTES_PER_SEC = 3400          # 24k mono ABR16k 实测码率（保守值）
MIN_CLIP_SEC = 0.45
_wnoise = None


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg):
    print(f"FATAL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# ---------- 游戏帧表 ----------

def load_table():
    with PK.open("rb") as f:
        f.seek(BASE)
        raw = f.read(4015 * 8)
    if struct.unpack_from("<I", raw, 0)[0] != 4015:
        die("帧表头校验失败（count!=4015）——现役包可能被改动，禁止继续")
    offs = [struct.unpack_from("<I", raw, i * 8 + 4)[0] + BASE for i in range(4015)]
    return offs


def slot_info(offs_sorted, slot):
    ao = offs_sorted[slot]
    i = offs_sorted.index(ao)
    gap = (offs_sorted[i + 1] - ao) if i + 1 < len(offs_sorted) else None
    with PK.open("rb") as f:
        f.seek(ao)
        hdr = f.read(32)
    if hdr[:4] != b"KOVS":
        die(f"slot {slot} 头部非 KOVS")
    return ao, struct.unpack_from("<I", hdr, 4)[0], gap


def extract_ogg(slot):
    """只读抽取 slot 的 ogg 流（内存中），带缓存。"""
    cache = DATA / "asr-cache" / f"slot{slot}.ogg"
    if cache.exists():
        return cache.read_bytes()
    offs = load_table()
    with PK.open("rb") as f:
        f.seek(offs[slot])
        hdr = f.read(32)
        decl = struct.unpack_from("<I", hdr, 4)[0]
        blob = hdr + f.read(decl)
    stream = bytearray(blob[32:32 + decl])
    for i in range(min(0x100, len(stream))):
        stream[i] ^= i
    if stream[:4] != b"OggS":
        die(f"slot {slot} 解码后非 OggS")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(bytes(stream))
    return bytes(stream)


# ---------- ASR ----------

_MODEL_CACHE = {}


def _model(name):
    if name in _MODEL_CACHE:
        return _MODEL_CACHE[name]
    deps = ASR_HOME / "deps"
    if deps.exists():
        sys.path.insert(0, str(deps))
    os.environ.setdefault("HF_HOME", str(ASR_HOME / "models"))
    from faster_whisper import WhisperModel
    kw = {}
    if os.environ.get("VJ_CPU_THREADS"):
        kw["cpu_threads"] = int(os.environ["VJ_CPU_THREADS"])
    m = WhisperModel(name, device="cpu", compute_type="int8",
                     download_root=str(ASR_HOME / "models"), **kw)
    _MODEL_CACHE[name] = m
    return m


def _decode_pcm(path):
    import numpy as np
    r = subprocess.run([FF, "-v", "error", "-i", str(path), "-f", "s16le",
                        "-ar", "16000", "-ac", "1", "-"], capture_output=True, timeout=60)
    if r.returncode != 0:
        die(f"ffmpeg 解码失败 {path}: {r.stderr[:200]}")
    return np.frombuffer(r.stdout, dtype="<i2").astype(np.float32) / 32768.0


def asr_slot_text(slot):
    """槽位文本（small 模型——base 会把人名念错：孔明→孫明、袁绍→原本初）。缓存。"""
    cache = DATA / "asr-cache" / f"slot{slot}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    m = _model("small")
    segs, _ = m.transcribe(_decode_pcm_bytes(extract_ogg(slot)), language="zh",
                           beam_size=5, vad_filter=False, condition_on_previous_text=False,
                           temperature=0.0)
    text = "".join(s.text for s in segs).strip()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text


def _decode_pcm_bytes(ogg):
    import tempfile, numpy as np, os
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tf:
        tf.write(ogg)
        tmp = tf.name
    try:
        return _decode_pcm(Path(tmp))
    finally:
        os.unlink(tmp)


def asr_pack_words(pack_dir):
    """包内词级转写（缓存 per 包名）。返回 {scene_no: {duration,text,sentences,words}}。"""
    key = re.sub(r"[^\w\u4e00-\u9fff]+", "_", pack_dir.name)
    cache = DATA / "asr-cache" / f"pack-{key}-words.json"
    existing = {}
    if cache.exists():
        existing = {int(k): v for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}

    needed_files = {k: v for k, v in SCENE_FILES.items() if (pack_dir / v).exists()}
    for no in range(1, 10):
        if no not in needed_files:
            die(f"语音包缺基础文件: {pack_dir / SCENE_FILES[no]}")

    if all(no in existing for no in needed_files):
        return {no: existing[no] for no in needed_files}

    m = _model("small")
    out = dict(existing)
    for no, fname in needed_files.items():
        if no in out:
            continue
        wav = pack_dir / fname
        segs, info = m.transcribe(_decode_pcm(wav), language="zh", beam_size=5,
                                  vad_filter=False, condition_on_previous_text=False,
                                  temperature=0.0, word_timestamps=True)
        seg_list = list(segs)
        words = [{"start": w.start, "end": w.end, "word": w.word}
                 for s in seg_list for w in (s.words or [])]
        out[no] = {"duration": info.duration,
                   "text": "".join(s.text for s in seg_list).strip(),
                   "sentences": [{"start": s.start, "end": s.end,
                                  "text": s.text.strip()} for s in seg_list],
                   "words": words}
        log(f"  pack ASR {fname}: {out[no]['text'][:30]}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


# ---------- 武将/语音库 ----------

def officers():
    return {o["name"]: o for o in json.loads(
        (DATA / "officers.json").read_text(encoding="utf-8"))["officers"]}


def voice_gate(name):
    """门禁1：导出表验证 voice 值与独占性。"""
    o = officers().get(name) or die(f"未知武将 {name}（见 officers.json）")
    d = EXPORT.read_bytes()
    Q0 = 0x60C1
    S = 0x1B0
    if len(d) < Q0 + 855 * S:
        die("共享剧本导出文件缺失或尺寸异常")
    voices = [d[Q0 + i * S - 8] for i in range(855)]
    v = voices[o["id"]]
    users = [i for i, x in enumerate(voices) if x == v]
    if v != o["voice"] or users != [o["id"]]:
        die(f"门禁1 FAIL：{name} 期望 V{o['voice']}，实测 V{v}，引用 {users}")
    log(f"门禁1 PASS：{name}(id={o['id']}) Voice={v}，855 武将中唯一引用")
    return o


def bankmap():
    return json.loads((DATA / "bank-map.json").read_text(encoding="utf-8"))


def save_bankmap(m):
    (DATA / "bank-map.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- locate ----------

def _is_marker(text, markers):
    return any(mk in text for mk in markers)


def _is_foreign(text):
    if text in SYSTEM_EXACT or len(text) <= 2 and text in SYSTEM_EXACT:
        return True
    return any(p in text for p in SYSTEM_PHRASES) or any(n in text for n in OTHER_NAMES)


def _scan(region_list, markers, label):
    hits = {}
    import bisect
    offs = load_table()
    sorted_offs = sorted(offs)
    for lo, hi in region_list:
        probes = list(range(lo, hi, STRIDE))
        log(f"  {label} 扫描 {lo}..{hi} 共 {len(probes)} 探针（stride {STRIDE}）")
        for s in probes:
            if s >= 4015:
                break
            t = asr_slot_text(s)
            if _is_marker(t, markers):
                hits[s] = t
                log(f"    命中 slot {s}: {t[:30]}")
    return hits


def _refine(start_hint, end_hint, markers, cap):
    """从锚点簇向外扩展到边界（遇他库/系统/占位停）。"""
    lo, hi = start_hint, end_hint
    while hi - end_hint < cap and hi + 1 < 4015:
        t = asr_slot_text(hi + 1)
        if _is_foreign(t) or t == "":
            break
        if _is_marker(t, markers):
            hi += 1
        else:
            hi += 1
    while start_hint - lo < cap and lo - 1 >= 0:
        t = asr_slot_text(lo - 1)
        if _is_foreign(t) or t == "":
            break
        if _is_marker(t, markers):
            lo -= 1
        else:
            lo -= 1
    return lo, hi


def cmd_locate(name):
    o = voice_gate(name)
    m = bankmap()
    if name in m and m[name].get("verified"):
        log(f"{name} 已定位: {m[name]}")
        return m[name]
    markers = o["markers"]
    bat = _scan(REGION_BATTLE, markers, "战斗区")
    dlg = _scan(REGION_DIALOG, markers, "对话区")
    if not bat or not dlg:
        die(f"未在默认区域找到 {name} 的自称锚字（战斗 {len(bat)}/对话 {len(dlg)} 命中）。"
            f"扩大 REGION_* 或核对 markers 后重试")
    b_lo, b_hi = _refine(min(bat), max(bat), markers, 40)
    d_lo, d_hi = _refine(min(dlg), max(dlg), markers, 75)
    entry = {"battle": [b_lo, b_hi], "dialogue": [d_lo, d_hi], "voice": o["voice"],
             "verified": True,
             "evidence": f"战斗锚 {sorted(bat)}；对话锚 {sorted(dlg)}；"
                         f"边界外核：{asr_slot_text(b_lo-1)[:12]} | {asr_slot_text(b_hi+1)[:12]}"
                         f" / {asr_slot_text(d_lo-1)[:12]} | {asr_slot_text(d_hi+1)[:12]}",
             "located_at": time.strftime("%Y-%m-%d %H:%M")}
    m[name] = entry
    save_bankmap(m)
    log(f"定位完成：战斗 {b_lo}-{b_hi}（{b_hi-b_lo+1} 槽），对话 {d_lo}-{d_hi}（{d_hi-d_lo+1} 槽）")
    log("⚠ plan 时请人工核对边界槽台词（首尾各 2 槽）再允许写包。")
    return entry


# ---------- plan ----------

def _bucket_of(text, buckets, where, has_ext=False):
    t = text.replace(" ", "")
    if has_ext and "rules_ext" in buckets:
        g = buckets.get("grunt_ext", buckets["grunt"])
        if len(t) <= g["max_text_len"]:
            return g["bucket"], g["file"]
        l = buckets.get("laugh_ext", buckets["laugh"])
        for pat in l["patterns"]:
            if pat in t and len(t) <= 8:
                return l["bucket"], l["file"]
        for r in buckets["rules_ext"]:
            if any(p in t for p in r["patterns"]):
                return r["bucket"], r["file"]
        fb = buckets.get("fallback_ext", buckets["fallback"])
        return "兜底", fb["battle" if where == "battle" else "dialogue"]

    rules = buckets["rules"]
    if len(t) <= buckets["grunt"]["max_text_len"]:
        return buckets["grunt"]["bucket"], buckets["grunt"]["file"]
    for pat in buckets["laugh"]["patterns"]:
        if pat in t and len(t) <= 8:
            return buckets["laugh"]["bucket"], buckets["laugh"]["file"]
    for r in rules:
        if any(p in t for p in r["patterns"]):
            return r["bucket"], r["file"]
    fb = buckets["fallback"]
    return "兜底", fb["battle" if where == "battle" else "dialogue"]


def _sentence_spans(pack_no, words, max_sec):
    """从词级时间戳拼出候选句段（整句优先，超长截前缀）。"""
    sents = []
    cur, c0 = [], None
    for i, w in enumerate(words):
        if c0 is None:
            c0 = w["start"]
        cur.append(w)
        is_last = (i == len(words) - 1)
        has_gap = False
        if not is_last:
            next_w = words[i + 1]
            if next_w["start"] - w["end"] >= 0.35 or next_w["word"].startswith(" "):
                has_gap = True
        if re.search(r"[。！？!?，,]", w["word"]) or has_gap or is_last:
            txt = "".join(x["word"] for x in cur).strip()
            if txt:
                sents.append({"t0": c0, "t1": w["end"], "text": txt})
            cur, c0 = [], None
    out = []
    for s in sents:
        if s["t1"] - s["t0"] < MIN_CLIP_SEC:
            continue
        if s["t1"] - s["t0"] <= max_sec:
            out.append(s)
        else:  # 截前缀
            acc = []
            for w in words:
                if w["start"] >= s["t0"] - 0.05 and w["end"] <= s["t0"] + max_sec:
                    acc.append(w)
                if acc and w["end"] - s["t0"] > max_sec - 0.15:
                    break
            if acc and acc[-1]["end"] - acc[0]["start"] >= MIN_CLIP_SEC:
                out.append({"t0": acc[0]["start"], "t1": acc[-1]["end"],
                            "text": "".join(x["word"] for x in acc)})
    return out


def cmd_plan(pack_dir, name):
    o = voice_gate(name)
    entry = bankmap().get(name) or die(f"{name} 未定位，先跑 locate")
    if not entry.get("verified"):
        die(f"{name} bank 条目未验证")
    buckets = json.loads((DATA / "buckets.json").read_text(encoding="utf-8"))
    offs_sorted = sorted(load_table())
    slots = (list(range(entry["battle"][0], entry["battle"][1] + 1)) +
             list(range(entry["dialogue"][0], entry["dialogue"][1] + 1)))
    # 门禁1b：槽区无同偏移别名
    off_list = load_table()
    dup = {}
    for i, ao in enumerate(off_list):
        dup.setdefault(ao, []).append(i)
    aliased = [s for s in slots if len(dup[off_list[s]]) > 1]
    if aliased:
        die(f"门禁1 FAIL：槽区存在同偏移别名 {aliased}，禁止施工")
    log(f"门禁1b PASS：{len(slots)} 槽无别名")
    pack = Path(pack_dir)
    if not pack.is_dir():
        die(f"语音包目录不存在: {pack}")
    words = asr_pack_words(pack)
    has_ext = (10 in words and 11 in words)
    run = OUTROOT / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    run.mkdir(parents=True, exist_ok=True)
    rows = []
    rot = {}
    for s in slots:
        where = "battle" if s <= entry["battle"][1] else "dialogue"
        text = asr_slot_text(s)
        bucket, fno = _bucket_of(text, buckets, where, has_ext=has_ext)
        _, decl, gap = slot_info(offs_sorted, s)
        cap = max(((gap or decl + 64) - 96) / BYTES_PER_SEC, MIN_CLIP_SEC)
        spans = _sentence_spans(fno, words[fno]["words"], cap)
        if not spans:
            fb = buckets.get("fallback_ext", buckets["fallback"]) if has_ext else buckets["fallback"]
            alt = fb["battle" if where == "battle" else "dialogue"]
            spans = _sentence_spans(alt, words[alt]["words"], cap)
            fno = alt
        idx = rot.get(fno, 0) % len(spans)
        rot[fno] = idx + 1
        pick = spans[idx]
        rows.append({"slot": s, "where": where, "orig_text": text, "bucket": bucket,
                     "scene_file": fno, "t0": round(pick["t0"], 3), "t1": round(pick["t1"], 3),
                     "cut_text": pick["text"], "cap_sec": round(cap, 2),
                     "gap": gap, "decl": decl})
    (run / "plan.json").write_text(json.dumps(
        {"officer": name, "pack": str(pack), "voice": o["voice"],
         "bank": entry, "slots": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    # 摘要
    from collections import Counter
    cb = Counter(r["bucket"] for r in rows)
    log(f"plan 完成 → {run/'plan.json'}")
    log(f"槽 {len(rows)} 个；分桶: {dict(cb)}")
    log("请人工抽查：① 边界槽（battle/dialogue 首尾各 2）orig_text 是否本武将；"
        "② cut_text 分配是否离谱。确认后再 build/write。")
    return run


# ---------- build / write ----------

def encode_clip(wav, t0, t1):
    dur = t1 - t0
    af = (f"afade=t=in:st=0:d=0.04,afade=t=out:st={max(dur-0.07,0):.3f}:d=0.07,"
          f"loudnorm=I=-16")
    p = subprocess.run([FF, "-v", "error", "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}", "-i", str(wav),
                        "-af", af, "-ar", "24000", "-ac", "1", "-c:a", "libvorbis",
                        "-b:a", "16k", "-f", "ogg", "-"], capture_output=True, timeout=120)
    if p.returncode != 0 or p.stdout[:4] != b"OggS":
        die(f"编码失败 {wav.name} {t0}-{t1}: {p.stderr[:200]}")
    return p.stdout


def ogg_seconds(ogg):
    p = subprocess.run([FF, "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ar", "24000",
                        "-ac", "1", "-"], input=ogg, capture_output=True, timeout=60)
    if p.returncode != 0:
        die("解码回检失败")
    return len(p.stdout) / 2 / 24000


def wrap_kovs(ogg):
    hdr = bytearray(32)
    hdr[0:4] = b"KOVS"
    struct.pack_into("<I", hdr, 4, len(ogg))
    struct.pack_into("<I", hdr, 0x10, 1)
    stream = bytearray(ogg)
    for i in range(min(0x100, len(stream))):
        stream[i] ^= i
    return bytes(hdr) + bytes(stream)


def _find_plan(run_or_latest, name):
    run = Path(run_or_latest)
    if not (run / "plan.json").exists():
        cands = sorted(OUTROOT.glob(f"{name}-*/plan.json"))
        if not cands:
            die(f"找不到 plan（{run}）")
        run = cands[-1].parent
        log(f"使用最新 plan: {run}")
    return run, json.loads((run / "plan.json").read_text(encoding="utf-8"))


def cmd_build(run_dir, name):
    run, plan = _find_plan(run_dir, name)
    pack = Path(plan["pack"])
    (run / "kovs").mkdir(exist_ok=True)
    (run / "originals").mkdir(exist_ok=True)
    offs_sorted = sorted(load_table())
    out = []
    for r in plan["slots"]:
        wav = pack / SCENE_FILES[r["scene_file"]]
        ogg = encode_clip(wav, r["t0"], r["t1"])
        secs = ogg_seconds(ogg)
        kovs = wrap_kovs(ogg)
        ao, decl, gap = slot_info(offs_sorted, r["slot"])
        if len(kovs) + 4 >= gap:
            die(f"门禁3 FAIL：slot {r['slot']} payload {len(kovs)} ≥ gap {gap}")
        pad_to = min(max(decl + 32, len(kovs) + 1), gap)
        payload = kovs + b"\x00" * (pad_to - len(kovs))
        bak = run / "originals" / f"slot{r['slot']}-orig.bin"
        if not bak.exists():
            with PK.open("rb") as f:
                f.seek(ao)
                bak.write_bytes(f.read(gap))
        (run / "kovs" / f"slot{r['slot']}.kovs").write_bytes(payload)
        out.append({"slot": r["slot"], "abs_off": f"0x{ao:X}", "pad_to": pad_to,
                    "kovs_bytes": len(kovs), "clip_sec": round(secs, 3), "gap": gap,
                    "payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "orig_sha256": hashlib.sha256(bak.read_bytes()).hexdigest()})
        log(f"  slot {r['slot']} {secs:.2f}s {len(kovs)}B pad={pad_to} gap={gap} OK")
    (run / "manifest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    log(f"门禁3a PASS：{len(out)} 载荷全部 ≤ 间隙；原文已备份 {run/'originals'}")


def _real_game_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq San13PK_tc.exe", "/FO", "CSV"],
                         capture_output=True, text=True, encoding="gbk", errors="replace").stdout
    for line in out.splitlines()[1:]:
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 5 and parts[0] == "San13PK_tc.exe":
            try:
                if int(parts[4].replace(" K", "").replace(",", "")) > 100000:
                    return int(parts[1])
            except ValueError:
                continue
    return None


def chunk_hashes(chunk=1 << 22):
    h = []
    with PK.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.append(hashlib.sha256(b).hexdigest())
    return h


def cmd_write(run_dir, name):
    run, plan = _find_plan(run_dir, name)
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    pid = _real_game_running()
    if pid:
        die(f"门禁3b FAIL：游戏正在运行（PID {pid}），请完全退出后重试")
    pre = chunk_hashes()
    CH = 1 << 22
    allow = set()
    for m in man:
        ao = int(m["abs_off"], 16)
        allow.update({ao // CH, (ao + m["pad_to"]) // CH})
    with PK.open("r+b") as f:
        for m in man:
            payload = (run / "kovs" / f"slot{m['slot']}.kovs").read_bytes()
            if hashlib.sha256(payload).hexdigest() != m["payload_sha256"]:
                die(f"载荷哈希不符 slot {m['slot']}")
            ao = int(m["abs_off"], 16)
            f.seek(ao)
            cur = f.read(len(payload))
            bak = (run / "originals" / f"slot{m['slot']}-orig.bin").read_bytes()[:len(payload)]
            if cur != bak:
                die(f"门禁3c FAIL：slot {m['slot']} 当前字节 != 备份原文（包被外部改动？）")
            f.seek(ao)
            f.write(payload)
        f.flush()
    post = chunk_hashes()
    if len(pre) != len(post):
        die("门禁4 FAIL：文件大小变化")
    changed = [i for i, (a, b) in enumerate(zip(pre, post)) if a != b]
    stray = [i for i in changed if i not in allow]
    if stray:
        die(f"门禁4 FAIL：越界变更块 {stray}")
    for m in man:
        ao = int(m["abs_off"], 16)
        payload = (run / "kovs" / f"slot{m['slot']}.kovs").read_bytes()
        with PK.open("rb") as f:
            f.seek(ao)
            if f.read(len(payload)) != payload:
                die(f"门禁4 FAIL：读回不一致 slot {m['slot']}")
            decl = struct.unpack_from("<I", payload, 4)[0]
        stream = bytearray(payload[32:32 + decl])
        for i in range(min(0x100, len(stream))):
            stream[i] ^= i
        if stream[:4] != b"OggS":
            die(f"门禁4 FAIL：文件内解码异常 slot {m['slot']}")
    with PK.open("rb") as f:
        f.seek(BASE)
        assert struct.unpack("<I", f.read(4))[0] == 4015
    log(f"门禁4 PASS：{len(man)} 槽写入并验证；变更块 {len(changed)}/{len(allow)} 受限；帧表未动")
    log(f"回滚：python voice_inject.py rollback {name} {run}")
    log("验收：冷启动游戏，目标武将全场景应为新声线；其他武将/系统通报必须原样。")


def cmd_rollback(name, run_dir):
    run = Path(run_dir)
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    offs = load_table()
    with PK.open("r+b") as f:
        for m in man:
            ao = int(m["abs_off"], 16)
            orig = (run / "originals" / f"slot{m['slot']}-orig.bin").read_bytes()
            f.seek(ao)
            cur = f.read(len(orig))
            if cur == orig:
                continue
            f.seek(ao)
            f.write(orig)
        f.flush()
    log(f"已按 originals 还原 {len(man)} 槽（幂等，已是原文的跳过）")


def cmd_list():
    olist = officers()
    bm = bankmap()
    packs = Path(os.environ.get("VJ_PACKS_DIR", str(Path.home() / "Downloads")))
    pack_dirs = [p.name for p in (packs.iterdir() if packs.is_dir() else [])
                 if p.is_dir() and p.name.endswith("语音包")
                 and (p / SCENE_FILES[1]).exists()]
    print("== 可让渡武将（专属语音库，已字节级验证独占）==")
    for n, o in olist.items():
        e = bm.get(n)
        if e:
            zones = [z for z in (e.get("battle"), e.get("battle_extra"), e.get("dialogue")) if z]
            st = "已定位 " + str(sum(zones, [])) + ("" if e.get("verified") else "（verified=false）")
        else:
            st = "未定位（先 locate）"
        print(f"  {n:<4} id={o['id']:<3} V{o['voice']:<2} {st}  {o['note']}")
    done = json.loads((DATA / "officers.json").read_text(encoding="utf-8"))["reference_done"]
    print(f"  参考基准：{done['name']} → {done['pack']}（{done['status']}）")
    print(f"== 可用语音包（{packs}，9 段制式）==")
    for p in pack_dirs:
        print(f"  {p}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    args = sys.argv[2:]
    if cmd == "list":
        cmd_list()
    elif cmd == "locate":
        cmd_locate(args[0])
    elif cmd == "plan":
        cmd_plan(args[0], args[1])
    elif cmd == "build":
        cmd_build(args[0], args[1])
    elif cmd == "write":
        cmd_write(args[0], args[1])
    elif cmd == "rollback":
        cmd_rollback(args[0], args[1])
    else:
        die(__doc__)
