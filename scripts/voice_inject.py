"""San13PK 专属语音替换流水线（san13pk-voice-injector）。

命令：
  list                         列出可用武将/语音包/bank 定位状态
  locate  <武将>               ASR 扫描定位该武将专属语音库槽区（缓存）
  plan    <语音包目录> <武将>  槽位分桶 + 包内词级ASR + 裁切分配 → plan.json + 报告
  build   <运行目录> <武将>  按 plan 编码打包 KOVS、备份原文、门禁1-3（不写包）
  write   <运行目录> <武将>  门禁3b/3c + 写入 + 门禁4 全验证
  rollback <武将> <运行目录>   从 originals 逐槽还原

四道门禁（全部 PASS 才落盘/才算成功）：
  门禁1 独占性：目标 Voice 库在 855 武将表中引用数=1；目标槽区无同偏移别名
  门禁2 定位：bank-map verified（≥2 个自称锚字命中 + 边界外侧非本库）
  门禁3 容量与基底：每槽 payload ≤ 物理间隙；游戏未运行；写前断言当前字节==备份
  门禁4 写后验证：逐槽读回哈希 + 文件内解码 + 4MB 块哈希受限差异 + 帧表未动 + 大小不变

铁律：只动已定位专属槽区；帧表(0x894B8600)永不动；只写 pk\\DATA1.S13；根目录包不碰；
      ffmpeg 经 stdout 管道写盘（AV 拦截旁路）；KOVS 头 size@4=流长、flag@0x10=1。
"""

import argparse
import bisect
import csv
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---- 路径配置（全部可用环境变量覆盖，见 README“环境变量”一节）----
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA = PROJECT_ROOT / "data"
ROOT = Path(os.environ.get("SAN13PK_HOME", r"D:\San13PK"))
PK = ROOT / "pk" / "DATA1.S13"
EXPORT = Path(os.environ.get(
    "SAN13PK_EXPORT",
    str(ROOT / "outputs" / "voice-bank" / "shared-scenario-export.s13")))
LAYOUT = json.loads(Path(os.environ.get("SAN13PK_LAYOUT", str(DATA / "layout.json"))).read_text(encoding="utf-8"))
BASE = int(LAYOUT["table_base"], 0)
SLOT_COUNT = LAYOUT["slot_count"]
OFFICER_BASE = int(LAYOUT["officer_base"], 0)
OFFICER_STRIDE = LAYOUT["officer_stride"]
OFFICER_COUNT = LAYOUT["officer_count"]
if BASE < 0 or SLOT_COUNT < 1 or OFFICER_BASE < 8 or OFFICER_STRIDE < 1 or OFFICER_COUNT < 1:
    raise ValueError("布局配置的偏移或记录数量无效")
_ff_env = os.environ.get("VJ_FFMPEG_DIR")
if _ff_env and (Path(_ff_env) / "ffmpeg.exe").exists():
    FFDIR = Path(_ff_env)
elif (PROJECT_ROOT / "bin" / "ffmpeg.exe").exists():
    FFDIR = PROJECT_ROOT / "bin"
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
SYSTEM_EXACT = {"报告", "罪", "罪名"}
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

def table_bytes(path):
    with path.open("rb") as f:
        f.seek(BASE)
        raw = f.read(SLOT_COUNT * 8)
    if len(raw) != SLOT_COUNT * 8 or struct.unpack_from("<I", raw, 0)[0] != SLOT_COUNT:
        die("帧表长度或计数与布局配置不符")
    return raw


def load_table():
    raw = table_bytes(PK)
    return [struct.unpack_from("<I", raw, i * 8 + 4)[0] + BASE for i in range(SLOT_COUNT)]


def slot_info(offs, slot):
    ao = offs[slot]  # 槽号只能索引原始帧表，不能索引物理地址排序表。
    physical = sorted(set(offs))
    i = bisect.bisect_right(physical, ao)
    end = physical[i] if i < len(physical) else PK.stat().st_size
    gap = end - ao
    if ao < BASE + SLOT_COUNT * 8 or gap < 32 or end > PK.stat().st_size:
        die(f"slot {slot} 地址或物理间隙超出文件边界")
    with PK.open("rb") as f:
        f.seek(ao)
        hdr = f.read(32)
    if hdr[:4] != b"KOVS":
        die(f"slot {slot} 头部非 KOVS")
    return ao, struct.unpack_from("<I", hdr, 4)[0], gap


def extract_ogg(slot):
    """从当前文件抽取槽位；不复用未绑定内容的旧 ogg 缓存。"""
    offs = load_table()
    ao, decl, gap = slot_info(offs, slot)
    if decl > gap - 32:
        die(f"slot {slot} 声明流长超出槽位容量")
    with PK.open("rb") as f:
        f.seek(ao + 32)
        stream = bytearray(f.read(decl))
    for i in range(min(0x100, len(stream))):
        stream[i] ^= i
    if stream[:4] != b"OggS":
        die(f"slot {slot} 解码后非 OggS")
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
    ogg = extract_ogg(slot)
    fingerprint = hashlib.sha256(ogg).hexdigest()
    cache = DATA / "asr-cache" / f"slot{slot}-{fingerprint}-small.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    m = _model("small")
    segs, _ = m.transcribe(_decode_pcm_bytes(ogg), language="zh",
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
    needed_files = {k: v for k, v in SCENE_FILES.items() if (pack_dir / v).exists()}
    for no in range(1, 10):
        if no not in needed_files:
            die(f"语音包缺基础文件: {pack_dir / SCENE_FILES[no]}")

    out = {}
    for no, fname in needed_files.items():
        wav = pack_dir / fname
        cache = DATA / "asr-cache" / f"pack-{no}-{file_hash(wav)}-small-words.json"
        if cache.exists():
            out[no] = json.loads(cache.read_text(encoding="utf-8"))
            continue
        m = _model("small")
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
        cache.write_text(json.dumps(out[no], ensure_ascii=False, indent=1), encoding="utf-8")
    return out


# ---------- 武将/语音库 ----------

def officers():
    return {o["name"]: o for o in json.loads(
        (DATA / "officers.json").read_text(encoding="utf-8"))["officers"]}


def voice_gate(name):
    """门禁1：导出表验证 voice 值与独占性。"""
    o = officers().get(name) or die(f"未知武将 {name}（见 officers.json）")
    d = EXPORT.read_bytes()
    Q0 = OFFICER_BASE
    S = OFFICER_STRIDE
    if len(d) < Q0 + OFFICER_COUNT * S:
        die("共享剧本导出文件缺失或尺寸异常")
    voices = [d[Q0 + i * S - 8] for i in range(OFFICER_COUNT)]
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

TRADITIONAL = str.maketrans({"趙": "赵", "雲": "云", "呂": "吕", "諸": "诸", "劉": "刘", "備": "备",
                           "張": "张", "飛": "飞", "關": "关", "孫": "孙", "權": "权", "龐": "庞",
                           "統": "统", "韋": "韦", "堅": "坚", "會": "会", "賈": "贾", "詡": "诩",
                           "擊": "击", "敗": "败", "敵": "敌", "將": "将", "虜": "虏", "報": "报",
                           "陣": "阵", "營": "营", "軍": "军", "國": "国", "辭": "辞", "傳": "传",
                           "馬": "马", "黃": "黄", "維": "维", "貂": "貂", "蟬": "蝉", "遼": "辽",
                           "穎": "颖", "賢": "贤", "師": "师", "蒼": "苍", "貧": "贫",
                           "惡": "恶", "來": "来", "鳳": "凤", "雛": "雏", "紹": "绍"})


def _is_marker(text, markers):
    text = text.translate(TRADITIONAL)
    return any(mk.translate(TRADITIONAL) in text for mk in markers)


def _is_foreign(text, markers):
    text = text.translate(TRADITIONAL)
    if _is_marker(text, markers):
        return False
    names = OTHER_NAMES + [mk for o in officers().values() for mk in o["markers"]]
    return (text in SYSTEM_EXACT or any(p.translate(TRADITIONAL) in text for p in SYSTEM_PHRASES)
            or any(n.translate(TRADITIONAL) in text for n in names))


def _scan(region_list, markers, label):
    hits = {}
    for lo, hi in region_list:
        probes = list(range(lo, hi, STRIDE))
        log(f"  {label} 扫描 {lo}..{hi} 共 {len(probes)} 探针（stride {STRIDE}）")
        for s in probes:
            if s >= SLOT_COUNT:
                break
            t = asr_slot_text(s)
            if _is_marker(t, markers):
                hits[s] = t
                log(f"    命中 slot {s}: {t[:30]}")
    return hits


def _refine(start_hint, end_hint, markers, cap):
    """从锚点簇向外扩展到边界（遇他库/系统/占位停）。"""
    lo, hi = start_hint, end_hint
    while hi - end_hint < cap and hi + 1 < SLOT_COUNT:
        t = asr_slot_text(hi + 1)
        if _is_foreign(t, markers) or t == "":
            break
        hi += 1
    while start_hint - lo < cap and lo - 1 >= 0:
        t = asr_slot_text(lo - 1)
        if _is_foreign(t, markers) or t == "":
            break
        lo -= 1
    return lo, hi


def cmd_locate(name):
    m = bankmap()
    if name in m and m[name].get("partial"):
        die(f"{name} 为不完整或交织语音库，不能自动定位或注入")
    o = voice_gate(name)
    if name in m and m[name].get("verified") and m[name].get("source_sha256") == bank_fingerprint(m[name]):
        log(f"{name} 已定位: {m[name]}")
        return m[name]
    markers = o["markers"]
    bat = _scan(REGION_BATTLE, markers, "战斗区")
    dlg = _scan(REGION_DIALOG, markers, "对话区")
    if len(bat) < 2 or len(dlg) < 2:
        die(f"未在默认区域找到 {name} 的自称锚字（战斗 {len(bat)}/对话 {len(dlg)} 命中）。"
            f"扩大 REGION_* 或核对 markers 后重试")
    b_lo, b_hi = _refine(min(bat), max(bat), markers, 40)
    d_lo, d_hi = _refine(min(dlg), max(dlg), markers, 75)
    entry = {"battle": [b_lo, b_hi], "dialogue": [d_lo, d_hi], "voice": o["voice"],
             "verified": False,
             "evidence": f"候选战斗锚 {sorted(bat)}；候选对话锚 {sorted(dlg)}；边界归属待人工确认。",
             "located_at": time.strftime("%Y-%m-%d %H:%M")}
    if name in m:
        previous = m[name]
        entry["reference"] = previous["reference"] if "reference" in previous else {"previous": previous}
    m[name] = entry
    save_bankmap(m)
    log(f"定位完成：战斗 {b_lo}-{b_hi}（{b_hi-b_lo+1} 槽），对话 {d_lo}-{d_hi}（{d_hi-d_lo+1} 槽）")
    log("候选不能直接写包；核对短应答簇与相邻武将后，运行 confirm-bank 明确确认两个区间。")
    return entry


def bank_slots(entry):
    if entry.get("partial") or entry["battle"] is None or entry["dialogue"] is None:
        die("不完整或交织语音库不能注入")
    slots = []
    for key in ("battle", "dialogue"):
        lo, hi = entry[key]
        if not (0 <= lo <= hi < SLOT_COUNT):
            die(f"{key} 槽区越界")
        slots.extend(range(lo, hi + 1))
    if len(slots) != len(set(slots)):
        die("战斗/对话槽区重叠")
    return slots


def bank_fingerprint(entry):
    digest = hashlib.sha256(table_bytes(PK))
    offs = load_table()
    slots = bank_slots(entry)
    neighbors = {s + d for s in slots for d in (-1, 0, 1) if 0 <= s + d < SLOT_COUNT}
    with PK.open("rb") as f:
        for slot in sorted(neighbors):
            ao, decl, gap = slot_info(offs, slot)
            if decl > gap - 32:
                die(f"slot {slot} 声明流长超出槽位容量")
            f.seek(ao)
            digest.update(struct.pack("<I", slot))
            digest.update(f.read(32 + decl))
    return digest.hexdigest()


def cmd_confirm_bank(name, battle, dialogue):
    m = bankmap()
    if name in m and m[name].get("partial"):
        die(f"{name} 的不完整语音库不能确认")
    o = voice_gate(name)
    entry = {"battle": list(battle), "dialogue": list(dialogue), "voice": o["voice"],
             "verified": True, "evidence": "使用者已核对槽区、短应答簇及两侧归属。"}
    entry["source_sha256"] = bank_fingerprint(entry)
    if name in m:
        previous = m[name]
        entry["reference"] = dict(previous["reference"]) if "reference" in previous else {"previous": previous}
        entry["reference"]["confirmed"] = {k: v for k, v in entry.items() if k not in ("source_sha256", "reference")}
    m[name] = entry
    save_bankmap(m)
    log(f"已确认 {name} 的槽区（仅适用于当前帧表及音频内容）")


# ---------- plan ----------

def _bucket_of(text, buckets, where, has_ext=False, markers=()):
    t = text.replace(" ", "")
    if _is_marker(t, markers):
        return "自报家门", 1
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
    if entry.get("source_sha256") != bank_fingerprint(entry):
        die("槽区未绑定当前游戏数据，请重新核对并 confirm-bank")
    offs = load_table()
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
        where = "battle" if entry["battle"][0] <= s <= entry["battle"][1] else "dialogue"
        text = asr_slot_text(s)
        bucket, fno = _bucket_of(text, buckets, where, has_ext=has_ext, markers=o["markers"])
        _, decl, gap = slot_info(offs, s)
        cap = max((gap - 96) / BYTES_PER_SEC, MIN_CLIP_SEC)
        spans = _sentence_spans(fno, words[fno]["words"], cap)
        if not spans:
            fb = buckets.get("fallback_ext", buckets["fallback"]) if has_ext else buckets["fallback"]
            alt = fb["battle" if where == "battle" else "dialogue"]
            spans = _sentence_spans(alt, words[alt]["words"], cap)
            fno = alt
        if not spans:
            die(f"slot {s} 没有符合容量的语音片段，请调整源素材")
        idx = rot.get(fno, 0) % len(spans)
        rot[fno] = idx + 1
        pick = spans[idx]
        rows.append({"slot": s, "where": where, "orig_text": text, "bucket": bucket,
                     "scene_file": fno, "t0": round(pick["t0"], 3), "t1": round(pick["t1"], 3),
                     "cut_text": pick["text"], "cap_sec": round(cap, 2),
                     "gap": gap, "decl": decl})
    (run / "plan.json").write_text(json.dumps(
        {"officer": name, "pack": str(pack), "voice": o["voice"],
         "bank": entry, "approved": False,
         "pack_sources": {str(no): file_hash(pack / fname) for no, fname in SCENE_FILES.items()
                          if (pack / fname).exists()},
         "slots": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
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
    p = subprocess.run([FF, "-v", "error", "-xerror", "-err_detect", "explode", "-i", "pipe:0", "-f", "s16le", "-ar", "24000",
                        "-ac", "1", "-"], input=ogg, capture_output=True, timeout=60)
    if p.returncode != 0 or not p.stdout:
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


def _find_plan(run_dir, name):
    run = Path(run_dir)
    if not (run / "plan.json").exists():
        die(f"找不到 plan（{run}），必须提供准确运行目录")
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    if plan["officer"] != name or name not in officers():
        die("plan 的武将与命令参数或白名单不符")
    return run, plan


def approval_hash(plan):
    content = {k: v for k, v in plan.items() if k not in ("approved", "approval_sha256")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cmd_approve(run_dir, name):
    run, plan = _find_plan(run_dir, name)
    plan["approved"] = True
    plan["approval_sha256"] = approval_hash(plan)
    (run / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"已确认 {name} 的 {len(plan['slots'])} 槽计划；此后修改内容须重新 approve/build")


def checked_plan(run_dir, name):
    run, plan = _find_plan(run_dir, name)
    if plan.get("approved") is not True or plan.get("approval_sha256") != approval_hash(plan):
        die("计划尚未确认或确认后被修改；核对后运行 approve")
    o = voice_gate(name)
    entry = bankmap()[name]
    if not entry.get("verified") or plan["bank"] != entry or plan["voice"] != o["voice"]:
        die("计划的槽区或语音库与当前已确认配置不符")
    slots = bank_slots(entry)
    if [r["slot"] for r in plan["slots"]] != slots:
        die("计划槽号与确认的槽区不一致")
    if entry.get("source_sha256") != bank_fingerprint(entry):
        die("槽区源内容已改变，请重新确认定位并生成计划")
    offs = load_table()
    if any(offs.count(offs[s]) != 1 for s in slots):
        die("目标槽位存在同偏移别名")
    return run, plan, offs


def cmd_build(run_dir, name):
    run, plan, offs = checked_plan(run_dir, name)
    pack = Path(plan["pack"])
    for no, fingerprint in plan["pack_sources"].items():
        if file_hash(pack / SCENE_FILES[int(no)]) != fingerprint:
            die("语音包在 plan 后已改变，请重新生成并确认计划")
    base_sha256 = file_hash(PK)
    (run / "kovs").mkdir(exist_ok=True)
    (run / "originals").mkdir(exist_ok=True)
    out = []
    for r in plan["slots"]:
        wav = pack / SCENE_FILES[r["scene_file"]]
        ogg = encode_clip(wav, r["t0"], r["t1"])
        secs = ogg_seconds(ogg)
        kovs = wrap_kovs(ogg)
        ao, decl, gap = slot_info(offs, r["slot"])
        if len(kovs) + 4 >= gap:
            die(f"门禁3 FAIL：slot {r['slot']} payload {len(kovs)} ≥ gap {gap}")
        pad_to = min(max(decl + 32, len(kovs) + 1), gap)
        payload = kovs + b"\x00" * (pad_to - len(kovs))
        bak = run / "originals" / f"slot{r['slot']}-orig.bin"
        with PK.open("rb") as f:
            f.seek(ao)
            original = f.read(gap)
        if bak.exists() and bak.read_bytes() != original:
            die("原备份与当前文件冲突，请使用新的运行目录")
        bak.write_bytes(original)
        (run / "kovs" / f"slot{r['slot']}.kovs").write_bytes(payload)
        out.append({"slot": r["slot"], "abs_off": f"0x{ao:X}", "pad_to": pad_to,
                    "kovs_bytes": len(kovs), "clip_sec": round(secs, 3), "gap": gap,
                    "payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "orig_sha256": hashlib.sha256(bak.read_bytes()).hexdigest()})
        log(f"  slot {r['slot']} {secs:.2f}s {len(kovs)}B pad={pad_to} gap={gap} OK")
    manifest = {"schema": 2, "officer": name, "plan_sha256": file_hash(run / "plan.json"),
                "base_sha256": base_sha256, "base_size": PK.stat().st_size,
                "table_sha256": hashlib.sha256(table_bytes(PK)).hexdigest(), "slots": out}
    (run / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    log(f"门禁3a PASS：{len(out)} 载荷全部 ≤ 间隙；原文已备份 {run/'originals'}")


def _real_game_running():
    result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                            text=True, encoding="mbcs", errors="strict")
    if result.returncode != 0:
        die(f"无法查询游戏进程：{result.stderr}")
    extra = {n.strip().casefold() for n in os.environ.get("SAN13PK_PROCESS_NAMES", "").split(",") if n.strip()}
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if not rows or any(len(row) < 2 or not row[1].isdigit() for row in rows):
        die("tasklist 返回格式异常，不能确认游戏已退出")
    for row in rows:
        if re.fullmatch(r"san13(?:pk)?(?:[_-].*)?\.exe", row[0], re.IGNORECASE) or row[0].casefold() in extra:
            return int(row[1])
    return None


def require_game_stopped():
    pid = _real_game_running()
    if pid is not None:
        die(f"游戏正在运行（PID {pid}），请完全退出后重试")


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 22):
            digest.update(block)
    return digest.hexdigest()


def read_manifest(run, name, plan):
    man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    if man["schema"] != 2 or man["officer"] != name or man["plan_sha256"] != file_hash(run / "plan.json"):
        die("manifest 版本、武将或计划指纹不符，请重新 build")
    slots = [m["slot"] for m in man["slots"]]
    if not slots or slots != [r["slot"] for r in plan["slots"]] or slots != bank_slots(plan["bank"]):
        die("manifest 槽号与计划/槽区不符")
    if man["base_size"] != PK.stat().st_size or man["table_sha256"] != hashlib.sha256(table_bytes(PK)).hexdigest():
        die("游戏文件大小或完整帧表已改变")
    offs = load_table()
    rows = []
    for m in man["slots"]:
        s = m["slot"]
        ao, _, gap = slot_info(offs, s)
        if offs.count(ao) != 1 or ao != int(m["abs_off"], 16) or gap != m["gap"]:
            die(f"slot {s} 的偏移、容量或唯一性已改变")
        payload = (run / "kovs" / f"slot{s}.kovs").read_bytes()
        original = (run / "originals" / f"slot{s}-orig.bin").read_bytes()
        if (not 32 < len(payload) == m["pad_to"] <= gap or len(original) != gap
                or hashlib.sha256(payload).hexdigest() != m["payload_sha256"]
                or hashlib.sha256(original).hexdigest() != m["orig_sha256"]):
            die(f"slot {s} 的载荷或原文备份损坏")
        rows.append((ao, original, payload))
    return man, rows


def unwrap_kovs(payload):
    if len(payload) < 32 or payload[:4] != b"KOVS":
        die("载荷不是完整 KOVS")
    decl = struct.unpack_from("<I", payload, 4)[0]
    if decl < 4 or decl > len(payload) - 32:
        die("KOVS 声明流长越界")
    stream = bytearray(payload[32:32 + decl])
    for i in range(min(0x100, len(stream))):
        stream[i] ^= i
    if stream[:4] != b"OggS":
        die("KOVS 解封后不是 OggS")
    return bytes(stream)


def verify_candidate(stage, rows, decode):
    if stage.stat().st_size != PK.stat().st_size or table_bytes(stage) != table_bytes(PK):
        die("候选文件精确大小或完整帧表不符")
    # 每个字节都与“原文件 + 明确目标范围”比较；同一 4MB 块内的越界变化也不能通过。
    with PK.open("rb") as source, stage.open("rb") as candidate:
        pos = 0
        while block := source.read(1 << 22):
            expected = bytearray(block)
            for ao, before, after in rows:
                lo, hi = max(pos, ao), min(pos + len(block), ao + len(after))
                if lo < hi:
                    expected[lo - pos:hi - pos] = after[lo - ao:hi - ao]
            if candidate.read(len(block)) != expected:
                die(f"候选文件存在非预期字节，块起点 {pos}")
            pos += len(block)
        if candidate.read(1):
            die("候选文件尾部存在额外字节")
    if decode:
        with stage.open("rb") as f:
            for ao, before, after in rows:
                f.seek(ao)
                ogg_seconds(unwrap_kovs(f.read(len(after))))


def replace_ranges(run, rows, action, source_sha256):
    # 同盘副本 + 原子替换：验证失败/进程中断发生在 replace 前时，正式文件从未写入。
    fd, stage_name = tempfile.mkstemp(prefix=PK.name + ".voice-", suffix=".tmp", dir=PK.parent)
    os.close(fd)
    stage = Path(stage_name)
    receipt = {"action": action, "state": "staging", "source_sha256": source_sha256,
               "stage_name": stage.name}
    state_path = run / "write-state.json"
    state_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    try:
        shutil.copyfile(PK, stage)
        with stage.open("r+b") as f:
            for ao, before, after in rows:
                f.seek(ao)
                if f.read(len(before)) != before:
                    die("候选基底与预检原文不符")
                f.seek(ao)
                f.write(after)
            f.flush()
            os.fsync(f.fileno())
        verify_candidate(stage, rows, decode=(action == "write"))
        receipt.update(state="ready", target_sha256=file_hash(stage))
        state_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        require_game_stopped()
        if file_hash(PK) != source_sha256:
            die("正式文件在候选制作期间被修改，已取消替换")
        os.replace(stage, PK)
        receipt["state"] = "committed"
        state_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    finally:
        stage.unlink(missing_ok=True)


def cmd_write(run_dir, name):
    require_game_stopped()
    run, plan, _ = checked_plan(run_dir, name)
    man, rows = read_manifest(run, name, plan)
    if file_hash(PK) != man["base_sha256"]:
        die("游戏文件已偏离 build 基底，请重新生成计划和构建")
    changes = []
    with PK.open("rb") as f:
        for ao, original, payload in rows:
            f.seek(ao)
            if f.read(len(original)) != original:
                die(f"写前原文不符，偏移 {ao}；没有写入任何槽位")
            ogg_seconds(unwrap_kovs(payload))
            changes.append((ao, original[:len(payload)], payload))
    replace_ranges(run, changes, "write", man["base_sha256"])
    log(f"写入完成：{len(rows)} 槽，全部预检、候选读回/解码和范围验证通过后原子替换")
    log(f"回滚：python voice_inject.py rollback {name} {run}")


def cmd_rollback(name, run_dir):
    require_game_stopped()
    run, plan = _find_plan(run_dir, name)
    man, rows = read_manifest(run, name, plan)
    source_sha256 = file_hash(PK)
    changes = []
    with PK.open("rb") as f:
        for ao, original, payload in rows:
            f.seek(ao)
            current = f.read(len(original))
            if current == original:
                continue
            if current != payload + original[len(payload):]:
                die(f"回滚冲突：偏移 {ao} 存在后续修改；没有恢复任何槽位")
            changes.append((ao, payload, original[:len(payload)]))
    if changes:
        replace_ranges(run, changes, "rollback", source_sha256)
    log(f"回滚完成：恢复 {len(changes)} 槽；已是原文的槽位跳过")


def cmd_status(run_dir, name):
    run, _ = _find_plan(run_dir, name)
    state = json.loads((run / "write-state.json").read_text(encoding="utf-8"))
    current = file_hash(PK)
    if current == state.get("target_sha256"):
        log(f"{state['action']} 已完整生效（即使完成记录在中断时未更新）")
    elif current == state["source_sha256"]:
        log("正式文件仍是操作前内容；可重新执行操作。临时文件如仍存在可手动清理：" + state["stage_name"])
    else:
        die("正式文件与操作前/候选指纹均不符，请保留现场检查，勿覆盖后续 MOD 修改")


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


def main():
    parser = argparse.ArgumentParser(description="San13PK 专属语音库替换工具（布局适用范围见 README）")
    parser.add_argument("--version", action="version", version="%(prog)s 0.2.1")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="列出武将、定位状态和语音包")
    locate = sub.add_parser("locate", help="ASR 生成待人工确认的候选槽区")
    locate.add_argument("name")
    confirm = sub.add_parser("confirm-bank", help="人工核对后明确确认两个闭区间")
    confirm.add_argument("name")
    confirm.add_argument("--battle", nargs=2, type=int, required=True)
    confirm.add_argument("--dialogue", nargs=2, type=int, required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("pack_dir")
    plan.add_argument("name")
    for command in ("approve", "build", "write", "status"):
        cmd_parser = sub.add_parser(command)
        cmd_parser.add_argument("run_dir")
        cmd_parser.add_argument("name")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("name")
    rollback.add_argument("run_dir")
    args = parser.parse_args()
    if args.command in (None, "list"):
        cmd_list()
    elif args.command == "locate":
        cmd_locate(args.name)
    elif args.command == "confirm-bank":
        cmd_confirm_bank(args.name, args.battle, args.dialogue)
    elif args.command == "plan":
        cmd_plan(args.pack_dir, args.name)
    elif args.command == "rollback":
        cmd_rollback(args.name, args.run_dir)
    else:
        {"approve": cmd_approve, "build": cmd_build, "write": cmd_write, "status": cmd_status}[args.command](args.run_dir, args.name)


if __name__ == "__main__":
    main()
