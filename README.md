# san13pk-voice-injector

三国志13威力加强版（San13PK）历史武将语音库替换工具。把 9 段制式语音包
（可带 10/11 扩展段）经过 ASR 定位、语义分桶、时长约束裁切和 KOVS 封装，
生成专属语音库的替换载荷。版本 **0.2.0，实验性工具**。

合成文件回归测试不代表任何游戏版本的实机验收。历史袁绍 97 槽案例是项目
历史记录，当前安装仍须重新定位、确认和验收。

## 适用范围与安装

- Windows、Python 3.10+，`pip install -r requirements.txt`。
- ffmpeg/ffprobe 放入 PATH，或设置 `VJ_FFMPEG_DIR`，或放在仓库 `bin/` 下。
- faster-whisper 首次运行会下载 small 模型；推理在本地进行，不调用在线语音 API。
  顶层依赖固定为 [1.2.1](https://pypi.org/project/faster-whisper/1.2.1/)，传递依赖尚未全量锁定。
- 默认布局仅对应曾观察的繁体中文 PK 数据包，不宣称其他语言、补丁或 MOD 通用。
  `data/layout.json` 保存帧表、槽数和共享剧本表布局；移植必须独立核实这些参数。
- 自行从持有的游戏导出共享剧本副本，放到 `SAN13PK_EXPORT` 指向的位置。
  未提供时不能通过独占性校验；仓库不提供游戏数据导出副本。

| 环境变量 | 默认值 / 作用 |
| --- | --- |
| `SAN13PK_HOME` | `D:\San13PK`，游戏根目录，只替换其 `pk\DATA1.S13` |
| `SAN13PK_EXPORT` | `<HOME>/outputs/voice-bank/shared-scenario-export.s13` |
| `SAN13PK_LAYOUT` | 仓库 `data/layout.json`；其他布局需自行验证 |
| `SAN13PK_ASR_HOME` | `<HOME>/outputs/asr`，模型及可选 deps |
| `SAN13PK_OUT` | `<HOME>/outputs/inject`，计划、载荷、原文备份、操作记录 |
| `SAN13PK_PROCESS_NAMES` | 额外游戏 exe 名，逗号分隔；用于改名的可执行文件 |
| `VJ_FFMPEG_DIR` | ffmpeg.exe/ffprobe.exe 所在目录 |
| `VJ_PACKS_DIR` | `~/Downloads`，仅供 `list` 本地查找语音包，不下载素材 |
| `VJ_CPU_THREADS` | faster-whisper CPU 线程数 |

## 使用流程

先完整备份游戏数据包，并在独立副本上操作。关闭游戏，留出至少一个完整
`pk/DATA1.S13` 副本大小的同盘空闲空间；空间不足会报错并保留正式文件。

```bash
cd scripts
python voice_inject.py --version
python voice_inject.py list
python scan_all.py                  # small 模型全表扫描，可指定 start/end
python voice_inject.py locate 孙策  # 仅生成候选，verified=false
```

人工核对候选两侧、首尾和短应答簇后，以实际安装的槽号运行：

```text
python voice_inject.py confirm-bank 孙策 --battle <战斗起点> <战斗终点> --dialogue <对话起点> <对话终点>
python voice_inject.py plan <语音包目录> 孙策
```

`data/bank-map.json` 的孙策/袁绍区间是历史参考，不能直接当作当前安装的确认。
`locate` 每个区域至少需要两处自称锚点，但启发式、繁简转换和名单仍不能证明
槽区独占；只有人工确认后才绑定当前帧表和音频指纹。张角属于 `partial`，
缺少已确认的独占对话库且战斗槽交织，禁止完整库定位/注入。

查看生成目录的 `plan.json`：核对武将、源文件、每个裁切区间、所有槽号，特别
是左右边界的短应答是否属于本武将。ASR 会误识别，自动分桶也会出现语义错位。
修改计划后，再明确确认并构建：

```text
python voice_inject.py approve <运行目录> 孙策
python voice_inject.py build <运行目录> 孙策
python voice_inject.py write <运行目录> 孙策
python voice_inject.py status <运行目录> 孙策
python voice_inject.py rollback 孙策 <运行目录>
```

`approve` 是使用者已审阅的明确声明，不替使用者判断台词归属。确认后修改计划
需要重新 approve/build。打错运行目录直接报错，不会自动选择“最新一次”。
旧版计划/manifest 不兼容，须重新确认槽区并 plan/approve/build；不自动迁移备份。

## 写入与回滚的实际边界

1. 写入时重新检查武将身份、共享剧本表中的 Voice 独占性、确认槽区、计划指纹、
   逻辑槽号到物理偏移的映射、别名、载荷/备份和完整基底指纹。
   全部预检通过前，不写任何游戏槽位。
2. 在正式包所在目录建立临时完整副本，向副本写入；核对精确大小、完整帧表和
   每个字节的预期变化，再从副本读回 KOVS，用 ffmpeg 严格解码。
3. 再查游戏进程和正式包基底；全部通过后用同盘 `os.replace` 替换正式文件。
   候选写入或验证失败不需要恢复正式包，因为正式包尚未修改。
4. `write-state.json` 记录替换前和候选指纹。中断后用 `status` 区分“未替换”、
   “已完整替换但完成记录未更新”和“外部修改”；确认已注入后可执行 rollback。
   这不是断电、磁盘损坏或所有文件系统场景的持久性保证，完整外部备份仍需保留。
5. 回滚检查游戏退出、目标身份、帧表、备份和全部当前槽位。若同槽有其他 MOD
   后续修改，拒绝覆盖，且不会先恢复部分槽。回滚也在副本上读回验证后替换。

进程检测匹配 San13/San13PK 及语言后缀，不使用内存阈值；查询失败或输出异常
时拒绝写入。被任意改名的游戏需配置 `SAN13PK_PROCESS_NAMES`，不靠猜测识别。
ASR 缓存绑定槽位音频或单个 WAV 内容及 small 模型名称；更换包或音频会失效，
旧版未绑定内容的缓存不再使用。源音频变化后须重新生成裁切计划。

## 格式来源与已有工具

KOVS 头及前 0x100 字节索引 XOR 是已有公开格式知识。致谢
[vgmstream 的 KOVS 实现](https://github.com/vgmstream/vgmstream/blob/master/src/meta/ogg_vorbis.c)，
其中 `kovs_ogg_decryption_callback` 实现了该异或规则；本项目不宣称它是原创发现。
San13Pictool 等社区工具已有手工导出/导入路径；本项目侧重 ASR、语义分配、
容量约束和可检查的替换流程。未附版本及可复现样本的第三方缺陷断言已移除。
格式与适用布局见 [docs/format.md](docs/format.md)。

## 语音包与使用责任

- 仅用于本人合法持有的游戏副本；不分发游戏包、抽取音频、语音素材或转写缓存。
- 素材须为自录、已获必要授权，或不模仿可识别真人且具有合法使用权的合成声音。
  使用者自行核对配音、录音和其他授权；仓库不链接第三方语音包。
- 不得用于冒充、骚扰或未经同意的性化内容。
- “单机 MOD”“极短片段”和工具许可证均不代表取得他人声音、素材的使用许可。
- 三国志13及相关商标归光荣特库摩；本项目与官方无关联。

## 开发验证

Python 3.11+ 和 ffmpeg 可运行：

```bash
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
```

测试只用临时合成包和内存夹具，不读取本机游戏文件，不下载 ASR 模型。
大部分业务测试替换 ASR/进程/编解码调用，另有真实 ffmpeg 合成音频编解码测试。
GitHub Actions 配置运行同一套测试，远端结果以实际运行记录为准。
版本变更见 [CHANGELOG.md](CHANGELOG.md)。许可证为原有 [MIT](LICENSE)。
