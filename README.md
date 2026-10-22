# san13pk-voice-injector

三国志13 威力加强版（San13PK）**历史武将专属语音库整体替换工具**：把一个 9 段制式语音包（01_初次拜访 ～ 09_生死羁绊，另可带 10/11 扩展段）自动分配、裁切、编码、封装，整体注入某个历史武将的专属语音库槽位，实现该武将**全场景换声线**。

核心特性：ASR 自动定位槽区 → 语义分桶 + 时长约束分配 → KOVS 封装 → 四道安全门禁 → 一键回滚。基准案例：袁绍，97/97 槽注入并实机验收通过（2026-10-05）。

> **Windows 专用**（写包门禁依赖 `tasklist` 检查游戏进程、ffmpeg 按 `.exe` 解析）。

## ⚠️ 免责声明

- 本工具仅用于修改**使用者本人合法持有**的游戏副本，供单机个人使用；请勿分发修改后的游戏文件。
- 本仓库**不包含任何游戏资产**：无游戏数据文件、无抽取音频、无语音包素材、无转写缓存。
- 写包前**务必备份 `pk\DATA1.S13`**（工具会在运行目录再做逐槽原文备份，但全局备份是你自己的责任）。
- 使用本工具造成的任何后果由使用者自行承担。

## 原理一图流

```
语音包(9~11 段 WAV)
   │  whisper small 词级 ASR（断句 + 时间戳）
   ▼
plan ── 槽位台词(全表 ASR 缓存) × 场景分桶规则(buckets.json) × 物理间隙容量约束
   │        → 每槽分配 [源文件, t0, t1] 裁切计划（人工核对）
   ▼
build ── ffmpeg 裁切重编码(24k mono vorbis ABR16k + loudnorm) → KOVS 32B 头 + 索引 XOR 封装
   ▼
write ── 四道门禁全 PASS 后写入 pk\DATA1.S13，逐槽读回验证
   ▼
实机验收 ── 不满意可 rollback 逐槽还原
```

格式细节（KOVS 头、索引 XOR、帧表布局、段界短应答簇）见 [docs/format.md](docs/format.md)。

## 环境要求

- Windows，Python 3.10+
- `pip install -r requirements.txt`（faster-whisper；首次定位会自动下载 whisper base/small 模型）
- ffmpeg / ffprobe：三选一 —— ① 加入 PATH；② 设 `VJ_FFMPEG_DIR` 指向其目录；③ 放一份到仓库 `bin/` 下（`.gitignore` 已忽略）
- 一份 San13PK 游戏安装（`pk\DATA1.S13`）
- **共享剧本导出副本**（门禁 1 依赖）：855 武将记录 × 每条 0x1B0 字节的数据块导出（武将号字节在记录内偏移 -8、相对基准 0x60C1），可用剧本编辑器或十六进制工具从你自己的游戏数据导出。放到 `SAN13PK_EXPORT` 指向的路径（默认 `<SAN13PK_HOME>\outputs\voice-bank\shared-scenario-export.s13`）。**没有它门禁 1 无法字节级复验，禁止写包。**

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SAN13PK_HOME` | `D:\San13PK` | 游戏根目录（其下应有 `pk\DATA1.S13`） |
| `SAN13PK_EXPORT` | `<HOME>\outputs\voice-bank\shared-scenario-export.s13` | 共享剧本导出副本路径 |
| `SAN13PK_ASR_HOME` | `<HOME>\outputs\asr` | ASR 模型与可选 deps 目录 |
| `SAN13PK_OUT` | `<HOME>\outputs\inject` | 运行目录（plan/kovs/备份/manifest） |
| `VJ_FFMPEG_DIR` | （PATH） | ffmpeg.exe / ffprobe.exe 所在目录 |
| `VJ_PACKS_DIR` | `~/Downloads` | `list` 命令扫描语音包的目录 |
| `VJ_CPU_THREADS` | （自动） | faster-whisper CPU 线程数 |

## 使用流程

```bash
cd scripts

# ① 首次：全表 4015 槽 ASR 扫描（base→small，约 2~4 小时，可中断续跑，可分段并行）
python scan_all.py            # 或 python scan_all.py 0 2000 分片

# ② 列出可让渡武将 / 语音包 / 定位状态
python voice_inject.py list

# ③ 定位某武将专属语音库槽区（自称锚字扫描 + 边界复核）
python voice_inject.py locate 张角

# ④ 生成分桶裁切计划（plan.json 给人看）
python voice_inject.py plan <语音包目录> 张角

# ⑤ 人工核对 plan（见下）→ 编码打包（不写包）+ 备份原文
python voice_inject.py build <运行目录> 张角

# ⑥ 四道门禁 + 写入 + 全验证
python voice_inject.py write <运行目录> 张角

# ⑦ 出问题逐槽还原
python voice_inject.py rollback 张角 <运行目录>
```

## 安全边界（违反即可能毁档/误伤）

1. **只允许注入 `data/officers.json` 白名单里已验证独占专属语音库的武将**。通用声线武将（Voice 0–13）绝不可用——会误伤全体大众脸 NPC。
2. 只写 `pk\DATA1.S13`；根目录 `DATA1.S13` 一律不碰；**帧表（0x894B8600）永不改动**。
3. 写包前游戏必须完全退出（门禁 3b 自检）。
4. **plan 结果必须人工核对并确认后才允许 build/write**——本工具刻意保留人在回路，不要跳过。

## 四道门禁

| # | 名称 | 内容 |
| --- | --- | --- |
| 1 | 独占性 | 目标 Voice 库在 855 武将表中引用数 = 1（对共享剧本导出副本字节级复验）+ 槽区无同偏移别名 |
| 2 | 定位 | bank-map verified：自称锚字在战斗/对话区各 ≥1 命中，边界外侧为他库/系统行 |
| 3 | 容量与基底 | 每槽 KOVS ≤ 物理间隙；游戏未运行；写前断言当前字节 == 备份原文 |
| 4 | 写后验证 | 逐槽读回哈希 + 文件内解码 + 4MB 块哈希受限差异 + 帧表未动 + 文件大小不变 |

任一 FAIL 立即中止，不落盘。

## plan 后人工核对点（必做）

1. plan.json 里 battle/dialogue **首尾各 2 槽**的 `orig_text` 是否确为目标武将台词（自报名/称号）；
2. **★ 段界短应答簇**：名将段结构是 `[前一位武将主台词 …] [本武将短应答簇 1~4 槽] [本武将主台词 …]`。静态边界只圈到主台词，**簇会被漏在范围外**，游戏读到这些槽时播的是前一位武将的原声（症状：该武将个别台词"还是原来的声音"，但字段/库/哈希全部正常）。查法：看计划**左界前 1~4 槽**的 `data/asr-cache/slotN.txt`，若是命令短应答（我知道了/交给我吧/明白…）则属本武将，应纳入计划；右界同理查 1~2 槽。判归属：看前一位武将在自己段内是否另有同套应答。详见 [docs/format.md](docs/format.md)；
3. 抽查 `cut_text` 分配有无明显荒谬（如笑槽分到长独白）——离谱的改 plan.json 对应行的 `scene_file/t0/t1` 再 build；
4. 语义错位是自动分配的正常代价，实听后可点名槽位微调。

## 已知坑（前人踩过，勿重复）

- **语音流必须做"索引 XOR"封装，漏了 = 静音**（字段/哈希全正常，就是没声）。见 format.md。
- San13Pictool 的导入功能会写坏 KOVS 头（流长=0），**禁用**其导入，用本工具。
- ffmpeg 直接写文件可能被杀软拦截——本工具一律走 stdout 管道，请勿改回。
- ASR 定位**必须用 small 模型**文本：base 会把人名听错（孔明→孫明）。

## License

MIT。仓库不含任何游戏资产；三国志13 及相关商标归属光荣特库摩（コーエーテクモゲームス），本项目与官方无关联。
