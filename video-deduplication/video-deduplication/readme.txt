================================================================
视频去重系统（video-deduplication）使用与开发说明
版本：v5（重写版 · GUI · 快通道 · 疑似档/容错/分组报告 · 字幕评分/多线程/WAL）
================================================================

本文件包含四部分内容：
  A. 设计思路 —— 为什么这样做（架构决策及依据）
  B. 实现思路 —— 怎么做的（流水线、算法、数据结构）
  C. 注意点   —— 运行 / 修改时必须小心的坑
  D. 代码导览 —— 给后续 AI 或开发者快速理解并修改代码的地图
（另有第 1 节使用说明、第 2 节文件清单，便于先跑起来再深入。）


================================================================
1. 使用说明
================================================================

1.1 环境准备
    Python 3.8+；依赖仅两个：
        pip install -r requirements.txt        # opencv-python + numpy
    OpenCV 读视频依赖 FFmpeg 解码后端（opencv-python 自带大部分解码器，
    特殊封装格式如 H.265/HEVC、ProRes 需要系统安装 FFmpeg）。

1.2 命令行用法（适合服务器 / NAS 宿主机）
    第 1 步 试运行（只出报告，绝不改动视频文件）：
        python3 main.py --video-dir /mnt/nas/videos
    第 2 步 人工复核 dedup_report.csv 的「待删除」行（或看 dedup_report.xlsx
        的「分组报告」sheet，按组浏览保留/删除依据）。
        - 不同意某行的判定？在「人工复核」列填 保留 / 待删除 / 唯一 即可改判；
        - 「疑似重复」行不会被自动删除，人工确认后用「人工复核」列定夺；
    第 3 步 确认无误后执行删除（建议配置回收目录，可恢复）：
        python3 main.py --video-dir /mnt/nas/videos --execute --trash-dir /mnt/nas/trash
        - 复核时改判过行的话，把编辑后的报告传回来，改判优先生效：
          python3 main.py --video-dir /mnt/nas/videos --execute \
              --trash-dir /mnt/nas/trash --reviewed-report dedup_report.csv

    常用参数（完整列表见 python3 main.py --help）：
        --db-path        指纹缓存库路径（放本地 SSD，默认 ./video_hashes.db）
        --report-path    CSV 报告输出路径（默认 ./dedup_report.csv）
        --interval       初筛固定抽帧间隔（秒，默认 30）
        --scene-threshold 场景检测阈值（越小越敏感，默认 12）
        --phash-threshold / --suspect-threshold / --final-threshold
                                初筛线 80 / 疑似线 85 / 判重线 90
        --trash-dir      回收目录：删除动作改为「移动」，保留相对路径结构
        --reviewed-report 人工复核后的 CSV：「人工复核」列的改判覆盖自动判定
        --no-fast-dedup  关闭文件级快速判重快通道（退回全帧级比对，结果等价）
        --multithread    多线程并行解码（可选优化：仅视频存本地 SSD 时用）
        --workers        多线程工作线程数（默认 4，配合 --multithread）
        --md5-chunk-size-mb  全文件 MD5 读块大小 MB（可选调优，默认 1；
                             万兆 SSD 可调 4）

1.3 图形界面用法（适合本地桌面）
        python3 gui.py
    界面三段式：参数配置 → 运行日志 → 复核结果表。
    - 「开始运行」在后台线程执行流水线，界面不卡死；
    - 「停止」可随时中断（协作式取消，已入库缓存保留，下次断点续跑）；
    - 结果表按处置决定着色（红=待删除 / 橙=疑似重复 / 绿=保留 / 灰=唯一
      / 紫=无法解码），可筛选；
    - 默认试运行；勾选「执行删除」才会动文件（无回收目录时会二次确认）；
    - 参数区含「文件级快速判重」开关（增量快通道，默认开，见 A.7）；
    - 参数区含「多线程并行解码」开关（可选，默认关，见 A.9）、
      工作线程数与 MD5 读块大小（可选调优）。
    无显示器的服务器环境 GUI 无法启动，请用命令行。

1.4 推荐工作流（安全第一）
    试运行 → 复核 CSV →（可选：--trash-dir 回收）→ --execute → 复查回收目录


================================================================
2. 文件与模块清单
================================================================
    main.py                 主程序入口：10 步流水线编排 + 命令行参数
    gui.py                  图形界面（tkinter）：操作面板 + 日志 + 结果查看
    config.py               全部可调参数（阈值 / 权重 / 分组区间 / 快通道开关）
    models.py               数据结构（元数据 / 指纹 / 比对结果 / 决策）
    utils.py                日志、SQLite 缓存读写（WAL）、dHash/pHash、文件指纹、
                            相似度、run_compute_jobs 并行执行器
    subtitle_detector.py    ★ 字幕检测评分：软字幕轨标记扫描 + 硬字幕启发式
    fast_dedup.py           ★ 文件级快速判重快通道：功能 1（字节级判重）+
                            功能 2（内容一致的指纹复用）
    threshold_detector.py   流式场景切换检测器（单遍顺序解码）
    frame_extractor.py      元数据读取 + 混合抽帧指纹扫描（单遍出两套指纹）
    metadata_processor.py   路径排序收集、大小/时长分组、候选对生成
    similarity_calculator.py 两级相似度判定（pHash 初筛 → dHash 精筛 → 融合）
    decision_maker.py       并查集聚组、保留评分（四维 + 字幕第五维）、人工改判、
                            dry-run/删除执行
    report_generator.py     CSV 复核报告 + Excel 双 sheet 报告（明细/分组）
                            + 人工改判读取
    requirements.txt        依赖清单
    readme.txt              本文件


================================================================
A. 设计思路（为什么这样做）
================================================================

A.1 硬件约束决定架构（最重要的一条）
    运行环境：视频文件在 NAS 的 16T 机械硬盘上（网络挂载）；
             运行程序的本机是 SSD。
    由此推导出三条硬性设计：
    (1) 视频解码必须单线程顺序读 —— 机械盘 + 网络链路下，随机寻道和并发
        读会互相拖慢，单线程顺序吞吐反而最高、行为最可预测。
        因此整个系统刻意不使用多线程/多进程处理视频帧。
    (2) 目录遍历按路径排序 —— 让磁头尽量顺序走（metadata_processor 中
        collect_video_paths 的 sorted 不是美化，是性能需求，勿删）。
    (3) 中间结果缓存在本地 SSD 的 SQLite —— 视频解码一次即可，dHash/pHash
        指纹、元数据全部入库；第二次运行零解码、秒级完成（实测 7/7 命中）。

A.2 分组过滤降复杂度（避免 O(N²) 全量比对）
    8000+ 视频两两比对是数千万对，不可接受。观察到重复视频几乎必然同时长
    （转码/压缩不改时长）或同文件大小（完全拷贝），因此：
        候选对 = 同时长组内配对 ∪ 同大小组内配对
    组外视频对直接跳过 —— 这是整个系统第一道、也是最有效的性能闸门。
    时长区间（config.DURATION_BUCKETS）按片库实际分布划分为 13 档。

A.3 混合抽帧策略（覆盖度与成本的折中）
    纯固定间隔抽帧：便宜，但可能漏掉转场前后的内容差异。
    纯场景检测抽帧：覆盖内容边界，但长视频全帧差分开销大。
    本系统两者结合：
        初筛指纹 = 固定间隔帧（0s, 30s, 60s ...，最多 8 帧）→ 算 pHash
        精筛指纹 = 场景切换点关键帧 → 算 dHash
    两套指纹在同一个视频的同一次顺序解码里产出（一遍解码、两套产物）。

A.4 两级相似度（先便宜后昂贵、先粗后细）
    pHash（感知哈希，DCT 低频）抗缩放/抗压缩，适合快速排除明显不同的视频：
        pHash 相似度 < 80 → 直接判不重复，跳过精筛（省掉大量精细比对）。
    dHash（差异哈希）对亮度/轻微形变更敏感，用在"疑似重复"的确认阶段。
    最终判定 = pHash × 0.7 + dHash × 0.3 ≥ 90 分才算重复。

A.5 人工复核是安全阀，不是可选项
    自动判重必然有误判（设计指标约 95.7% 准确率），而误删不可恢复。
    因此流程被刻意设计为两段式：
        默认 dry-run 只产出 CSV 复核报告（含每个分数和判定依据）；
        人类确认后显式加 --execute 才真正删除；
        且支持 --trash-dir 移入回收目录而非硬删，留恢复余地。
    修改代码时不得弱化这一语义（见 C.1）。

A.6 可解释性设计
    报告里每个视频都有「判定依据」文本：比对阶段（初筛/精筛分数）+
    保留评分四个分项 + 组内分差。让复核者能回答"为什么删它、为什么留它"，
    出问题能定位到是阈值问题还是评分问题。
    报告另有「判定来源」列（帧级比对 / 文件级快速判重 / 未比对）与
    「人工复核」列（填写即改判），快判与精判一目了然、复核可落地。

A.7 文件级快速判重快通道（增量场景，fast_dedup.py）
    场景：片库已跑过一遍（缓存库有历史结果），之后同目录又新增了视频。
    此时逐个解码新视频再帧级比对太贵，而「字节级相同」的重复有更便宜的判法。
    三级判据（一级不过即退回正常帧级比对）：
        1. 粗筛 —— (文件大小, 时长, 采样指纹) 三项一致；
           采样指纹 = 头/中/尾各 4KB + 文件大小的 MD5（只读 12KB，近乎免费）；
        2. 终审 —— 全文件 MD5 一致（顺序读不解码，仅对粗筛命中的少量候选）；
        3. 兜底 —— 任何不确定 → 原有帧级两级比对（最慢但最稳）。
    为什么不用「前 128 字节 MD5」：MP4 头部（ftyp/moov）在批量导出时高度
    雷同，只比头部存在真实碰撞风险 → 误判重复 → 误删不可逆。采样指纹+
    全文件 MD5 把碰撞概率压到可忽略。
    两个子功能：
        功能 1 判重：全量 MD5 相同 ⇒ 字节级重复簇。簇内「老件」（历史已处理
            或修改时间更早）保留，新增/副本件直接预判待删除 —— 零解码零比对；
        功能 2 复用：(文件名, 大小, 采样指纹) 命中历史指纹记录 ⇒ 判定是
            「之前处理过的同一视频」（复制/移动/换路径），直接复用历史
            dHash/pHash，零解码。移动件（旧路径已消失）只复用指纹、
            不会误判为重复 —— 盘上只剩一份的文件永远不进删除清单。
    安全阀与主线一致：快判结果照样进 CSV 复核报告（判定依据明确标注
    「文件级快速判重（全量 MD5 一致，未经帧级比对）」），默认 dry-run，
    FAST_DEDUP_ENABLED=False / --no-fast-dedup 可整体关闭。

A.8 三级结论与复核增强（v4）
    (1) 疑似重复中间档 —— 相似度落在「疑似线 85 ~ 判重线 90」之间的对，
        既不误删（不进删除清单）也不放过（报告单列「疑似重复」交人工
        判断）。这是误判漏网区：没有这一档时它们会混进「唯一」里没人看。
        阈值 config.SUSPECT_SIMILARITY_THRESHOLD，CLI --suspect-threshold。
    (2) 无法解码容错 —— 损坏/格式不支持的视频不再中断流水线，也不参与
        比对，报告单列「无法解码」（紫色），永不进删除清单，交人工检查。
    (3) 分组报告 sheet —— CSV 之外输出同名 .xlsx：「明细报告」sheet 与
        CSV 同构；「分组报告」sheet 按「待删除视频组 N → 保留行（保留依据）
        → 删除行（删除依据）」排布，附疑似/无法解码清单与释放空间统计，
        复核时先看它再看明细。
    (4) 日志轮转 —— RotatingFileHandler 单文件 5MB、保留 3 个归档
        （config.LOG_MAX_BYTES / LOG_BACKUP_COUNT），长期跑批不撑爆磁盘。

A.9 字幕得分第五维（v5，subtitle_detector.py）
    需求：同一视频的「有字幕版」与「无字幕版」并存时，优先保留有字幕版。
    落地：对时长 > 10 分钟（config.SUBTITLE_CHECK_MIN_SEC）的视频计算
    「字幕得分」（0-100），作为保留评分的第五维权重（config.SUBTITLE_WEIGHT
    默认 0.15，其余四维等比缩放到 0.85）。组内自动优先保留有字幕版本。
    检测两条路径（成本从低到高，均不增加解码遍数）：
        软字幕（字幕轨）—— 零解码：读文件头尾各 2MB 原始字节，搜容器字幕流
        标记（MP4: tx3g/sbtl/subp/c608/c708；MKV: S_TEXT//S_HDMV//S_VOBSUB），
        命中 ⇒ 100 分。头尾都扫是因为 MP4 的 moov 原子位置不确定。
        硬字幕（烧录）—— 复用已抽取的初筛帧：底部 20% 条带转灰度 → Canny
        → 边缘像素占比（字幕文字 2%~8%，普通底部 < 2%），归一化到 0-100。
    边界口径：组内全员都有字幕得分才启用第五维（组内口径一致，避免跨口径
    混算）；任一成员未计算（≤10 分钟或缓存旧行）→ 全组退回四维评分。
    这是启发式评分（复杂底部纹理可能虚高），故只占 15% 权重、不单独决定
    删留；报告「字幕得分」列与判定依据分项中可见。

A.10 多线程可选开关（v5，SSD 优化）与 WAL
    背景：默认单线程是 NAS 机械盘的硬约束（A.1）；但视频存本地 SSD 时
    顺序读的约束不存在，解码可并发。因此提供**可选**多线程模式：
        config.MULTITHREAD_ENABLED（默认 False）/ CLI --multithread /
        GUI 开关；线程数 config.WORKER_THREADS / --workers（默认 4）。
    并发边界（重要）：只并行「纯计算」环节（元数据读文件头、采样指纹、
    全文件 MD5、解码抽帧）；SQLite 读写始终在主线程串行（连接非线程安全）。
    线程而非进程：OpenCV 解码与文件 IO 释放 GIL，线程池有真实并行度；
    多进程反而要序列化 numpy 帧、复制内存。
    实现集中在 utils.run_compute_jobs（顺序/线程池统一入口，带 cancel_check
    探针），main 与 fast_dedup 的各阶段调用它，worker 只做计算不碰库。
    一致性保证：多线程只改变执行顺序，不改变判定结果（已测试单/多线程
    处置决定完全一致）。
    SQLite WAL：缓存库以 PRAGMA journal_mode=WAL + synchronous=NORMAL
    打开 —— 崩溃（断电/强杀）后重开自动恢复，8000 视频的指纹缓存
    （重建要几小时解码）值得这一行 PRAGMA 保护。

A.11 全文件 MD5 读块调优（v5，可选）
    快通道终审的全文件 MD5 是分块顺序读（utils.compute_file_md5）。
    读块大小 config.MD5_CHUNK_SIZE（默认 1MB）/ CLI --md5-chunk-size-mb /
    GUI「MD5 读块(MB)」可调：千兆网络 1MB 已近最优；万兆 SSD 可调 4MB
    减少系统调用次数。仅影响吞吐不影响结果（不同块大小 MD5 一致，已测试）。


================================================================
B. 实现思路（怎么做的）
================================================================

B.1 十步流水线（main.run_pipeline）
    ① 收集视频路径并排序               metadata_processor.collect_video_paths
    ② 读元数据（缓存优先，只读文件头） main.load_metadata_with_cache
    ③ 内容采样指纹（缓存优先，读 3 小块）fast_dedup.compute_sample_hashes
    ④ 文件级快速判重（可关）           fast_dedup.analyze_content_clusters
    ⑤ 分组 + 生成候选对（剔除字节级簇内对）metadata_processor.*
    ⑥ 候选视频抽指纹（缓存/内容复用优先，单遍解码；孪生件零解码别名）
                                         main.extract_fingerprints_with_cache
    ⑦ 两级相似度比对 + 文件级合成比对   similarity_calculator / fast_dedup
    ⑧ 重复对聚成重复组（并查集）        decision_maker.cluster_duplicate_groups
    ⑨ 四维保留评分 + 组内决策（副本件不参与竞争）decision_maker.make_retention_decisions
       （+ 人工改判覆盖 apply_review_overrides，如传入 --reviewed-report）
    ⑩ 报告（CSV + Excel 分组 sheet）→（人工复核）→ 删除执行
       report_generator / decision_maker.apply_deletion
    其中 ②③⑥ 三步是 NAS 慢 IO 的集中地，全部带 SQLite 缓存短路。

B.2 关键算法速查
    dHash（utils.dhash）      图缩到 9x8 灰度 → 每行相邻像素比较大小 → 64bit 串
    pHash（utils.phash）      图缩到 32x32 灰度 → DCT → 取左上 8x8 低频
                              → 与均值比较 → 64bit 串
    相似度（utils.hash_similarity）= (1 - 汉明距离/64) × 100，统一 0-100 分制
    ThresholdDetector（threshold_detector.py）
                              流式相邻帧灰度平均绝对差 + 淡入淡出补偿：
                              adjusted(i) = diff(i-1,i) + mean(diff(i-2,i-1),
                                          diff(i,i+1)) × fade_bias/100
                              adjusted > threshold 且距上一切换点 > min_scene_len
                              帧 → 判定 i 为切换点。只保留最近 3 帧灰度，
                              内存恒定，整段视频只顺序读一遍。
    关键帧对齐（similarity_calculator.match_frame_pairs）
                              普通视频：时间戳就近匹配（容差 3s）；
                              短视频对：按序号对齐（首对首/中对中/尾对尾）。
    并查集（decision_maker）   路径压缩 + 按秩合并；解决 A-B、B-C 传递成组。
    保留评分（decision_maker.calculate_retention_score）
                              四维基础分 = 内容差异×0.4 + 码率×0.3 + 分辨率×0.2
                                     + 入库时间×0.1（各项均已归一到 0-100）
                              字幕第五维（组内全员有字幕得分时启用）：
                                     总分 = 四维基础分×0.85 + 字幕得分×0.15
                              内容差异 = 100 - 与组内其他成员的平均相似度
                              码率/分辨率：线性归一到 10Mbps / 1080p 参考
                              入库时间：组内相对排名（最早 100 分，最晚 0 分）
                              字幕得分：软字幕轨=100，否则硬字幕启发式（见 A.9）
                              组内总分最高者保留，其余进删除清单；
                              并列时依次比较 码率 → 分辨率 → 修改时间更早。

B.3 短视频规则（用户约定）
    时长 ≤ 30 秒（config.SHORT_VIDEO_MAX_SEC）的视频：
      - 分组：全部归入同一个「30秒内组」；
      - 抽帧：固定间隔法抽不出有效帧，改取「首 / 中 / 尾」3 帧兜底，
              初筛、精筛两套指纹都来自这 3 帧；
      - 比对：按序号语义对齐（首对首、中对中、尾对尾）。

B.4 SQLite 缓存（utils.py，表 video_cache）
    一行一个视频：元数据（大小/时长/分辨率/帧率/码率/修改时间）
                  + 文件内容指纹（sample_hash 采样指纹 / full_md5 全文件 MD5）
                  + 两套图像指纹（fixed_* / scene_*，逗号分隔序列化）
                  + subtitle_score 字幕得分 + fingerprints_ready 标记。
    新鲜度校验：文件大小 + 修改时间都未变才复用；文件被替换自动重算
    （save_metadata_row 更新时会作废 sample_hash/full_md5，等待重算）。
    旧版库（无内容指纹列 / 无字幕列）打开时自动 ALTER TABLE 补列，不丢数据。
    WAL 模式打开（journal_mode=WAL + synchronous=NORMAL），崩溃安全。
    批量提交（config.DB_COMMIT_EVERY）减少 fsync。

B.5 并发与线程模型
    流水线本体：默认单线程顺序处理（NAS 约束，刻意如此）；
    --multithread（仅 SSD 场景）下，纯计算环节经 utils.run_compute_jobs
    交给线程池并发（元数据读头 / 采样指纹 / 全文件 MD5 / 解码抽帧），
    SQLite 读写始终主线程串行；worker 不得触碰连接（红线）。
    GUI：业务跑在后台守护线程，日志经 queue.Queue 桥接到主线程，
    tkinter 控件只在主线程访问；「停止」是协作式取消
    （threading.Event → cancel_check 探针 → PipelineCancelled），
    中断点位于每个视频/每对候选粒度，已入库缓存保留，下次续跑。

B.6 快通道实现要点（fast_dedup.py + main 里的接线）
    - compute_sample_hashes：全量视频的采样指纹（缓存命中则零 IO）；
    - analyze_content_clusters：
        粗筛分组 key = (file_size, round(duration,3), sample_hash)
        → 组内 _load_or_compute_full_md5（先查缓存列，缺才顺序读整文件）
        → MD5 相同者成簇 → _pick_survivor 选保留代表（历史已处理 > mtime 早 > 路径序）
        → 其余进 forced_deletes（预判待删除）+ alias_map（孪生 → 代表）
        → 簇内完全图合成 PairComparison（final=100, is_file_level=True）；
    - 帧级候选对会剔除簇内对（已由文件级定案），并查集统一合并两类比对；
    - extract_fingerprints_with_cache 三级取数：
        路径缓存 → find_content_reuse（功能 2，(文件名,大小,采样指纹) 查历史
        指纹行）→ 解码；解码对象只算簇代表，孪生件 dataclasses.replace
        复制指纹并改归属路径（video_path），实现「副本件零解码」；
    - 决策：forced_deletes 成员不参与保留竞争（直接 DELETE，评分照常列出
      备查），簇代表正常竞争；来源标注见 decision_maker._decision_source。


================================================================
C. 注意点（坑与边界）
================================================================

C.1 不可弱化的安全语义（改代码红线）
    - apply_deletion 的默认行为必须是 dry-run（execute=False 不动文件）；
    - CSV 报告必须在删除动作之前生成（它是复核依据）；
    - --execute 且无 --trash-dir 时必须打印醒目警告（GUI 里是二次弹窗确认）。

C.2 NAS / IO 相关
    - 视频存 NAS 机械盘时不要开 --multithread —— 网络机械盘上并发随机
      寻道更慢；性能瓶颈的正确解法是"少解码"（缓存）而不是"并发解码"。
      只有视频存本地 SSD 时才建议开启多线程（A.10）。
    - collect_video_paths 里的 sorted(...) 是顺序读保证，勿删。
    - 多线程 worker 里禁止碰 SQLite 连接（非线程安全）—— 一律
      run_compute_jobs 算完回主线程写库（红线）。
    - 帧数未知的容器（某些流式封装）走动态抽帧兜底；时长=0 的极端文件
      会被当作短视频处理（首/中/尾退化为首/尾）。

C.3 OpenCV 相关
    - CAP_PROP_BITRATE 在部分后端返回 0 或不可靠（单位 kbps），
      代码里已做 None 容错（码率得分记 0），报告中该列为空。
    - CAP_PROP_FRAME_COUNT 可能不准确（VBR 流），影响时长分组的边界
      判定，一般误差可容忍。
    - cv2.VideoCapture 打不开的损坏文件不再抛异常：元数据阶段返回
      decode_error 标记 → 报告单列「无法解码」，不参与分组/比对/删除。

C.4 判定精度相关
    - 相似度与评分全部是 0-100 分制（历史版本 0-1/0-100 混用是 bug，
      已统一，新代码不要再引入 0-1 尺度）。
    - 阈值 80/85/90（初筛/疑似/判重）是经验值：调低判重线会增加误删风险，
      调高会漏检；疑似线决定「人工复核工作量」—— 调低则疑似行变多。
      建议在片库上先 dry-run 看报告分布再调。
    - 时长分组区间（config.DURATION_BUCKETS）要与片库分布匹配；
      区间过大 → 组内比对对数暴涨；过小 → 近似重复跨组漏检
      （同大小配对通道可部分兜住完全拷贝）。
    - 创建时间在 NAS 上取的是「修改时间」（mtime），不是真正生日
      （ctime 在 Linux 是 inode 变更时间，不可靠）。

C.7 字幕检测注意点
    - 字幕得分是启发式（软字幕容器标记 + 底部边缘密度），不是 OCR：
      底部有滚动条/密集纹理的画面可能虚高；纯色字幕背景可能偏低。
      因此只作第五维权重（15%），不单独决定删留。
    - 软字幕标记扫描用裸字节搜索，极小概率被压缩数据里的巧合字节串
      误触发（tx3g 等 4 字节标记）；误触发的后果只是该视频字幕得分虚高，
      不影响判重正确性。
    - 字幕得分只对 >10 分钟视频计算（用户约定），且只在组内全员有得分
      时启用第五维 —— 改 SUBTITLE_CHECK_MIN_SEC 时留意口径一致性。
    - 字幕得分随指纹入缓存（subtitle_score 列）；旧缓存库自动补列后，
      已有行该列为 NULL（视为未计算），重算后自动填充。

C.5 依赖与平台
    - Windows 运行注意路径分隔符（本代码全程用 os.path，无需担心）
      与 GUI 的 os.startfile 行为差异。
    - 多进程写 SQLite 会锁竞争 —— 本系统单线程写入天然规避；
      若未来引入多进程，必须换 PostgreSQL 或加写队列。

C.6 快通道（fast_dedup）特有注意点
    - 功能 1 只在「盘上仍存在的视频」之间判重。缓存库有历史记录但文件
      已消失（被移走/删除）时，新出现的同内容文件是唯一副本，不会被
      误判删除 —— 这是有意的安全语义，不要改成"对照缓存库全量判重"。
    - 功能 2 只认 fingerprints_ready=1 的历史行；文件内容变了的行会在
      元数据阶段被作废（sample_hash 置空），不会把旧指纹错配给新内容。
    - 全文件 MD5 只对粗筛命中的候选计算（顺序读不解码）；不要"优化"成
      全库都算一遍 —— 增量场景下那才是大 IO。
    - 旧版缓存库（升级前生成）没有 sample_hash：首次跑批会为全部文件补
      算采样指纹（每个文件 3 次小读），此后增量运行只有新文件付出代价。
      期间「升级前移动过的文件」无法走功能 2 复用（历史行无采样指纹），
      会退回正常解码一次 —— 一次性代价，不影响正确性。
    - 判重/复用的三级判据顺序（粗筛 → 全量 MD5 → 帧级兜底）不要精简；
      跳过全量 MD5 终审会把「前 128 字节」级别的碰撞风险放回来。


================================================================
D. 代码导览（给后续 AI / 开发者：改代码先看这里）
================================================================

D.1 九条不变式（任何修改都必须保持）
    1. 相似度与评分统一 0-100 分制。
    2. 短视频（时长 ≤ 30s）= 单独一组 + 首/中/尾 3 帧兜底 + 序号对齐。
    3. 视频解码默认单线程、顺序读、目录遍历排序（NAS 机械盘约束）；
       --multithread 仅是 SSD 场景的可选并行，不得改变判定结果。
    4. 缓存以 (path, file_size, file_mtime) 校验新鲜度；指纹与元数据同表。
    5. 报告先于删除生成；默认 dry-run；删除可走回收目录。
    6. 文件级判重必须「采样指纹粗筛 + 全文件 MD5 终审」双关全过才生效；
       只判「盘上仍存在的视频」，唯一副本永不进删除清单（见 C.6）。
    7. 快判结果必须同样进 CSV 复核报告并标注来源（文件级快速判重），
       不得静默删除；forced_deletes 的评分照常列出供人工复核改判。
    8. 只有 action=DELETE 的行会进删除清单；SUSPECT（疑似）/ CORRUPT
       （无法解码）/ UNIQUE 永不自动删，只能由人工复核改判升级。
    9. 多线程 worker 禁止触碰 SQLite 连接；库读写只在主线程；
       字幕第五维只在「组内全员有字幕得分」时启用（组内口径一致）。

D.2 常见修改任务 → 改哪里（速查表）
    ┌────────────────────────────┬────────────────────────────────────┐
    │ 想做的事                   │ 修改位置                           │
    ├────────────────────────────┼────────────────────────────────────┤
    │ 调判重/初筛/疑似阈值、抽帧间隔│ config.py 默认值；GUI/CLI 参数覆盖 │
    │                            │ （疑似线 SUSPECT_SIMILARITY_        │
    │                            │ THRESHOLD / --suspect-threshold）   │
    │ 改时长分组区间             │ config.DURATION_BUCKETS            │
    │ 改保留评分维度或权重       │ config.WEIGHT_* / SUBTITLE_WEIGHT + │
    │                            │ decision_maker.calculate_retention │
    │                            │ _score / _score_bitrate 等         │
    │ 调字幕检测（阈值/启停）    │ subtitle_detector.py +             │
    │                            │ config.SUBTITLE_*（见 A.9/C.7）    │
    │ 开/关多线程、调线程数      │ config.MULTITHREAD_ENABLED /       │
    │                            │ WORKER_THREADS；CLI --multithread/ │
    │                            │ --workers；GUI 开关（见 A.10）     │
    │ 调 MD5 读块大小            │ config.MD5_CHUNK_SIZE 或           │
    │                            │ CLI --md5-chunk-size-mb（见 A.11） │
    │ 换/改哈希算法              │ utils.dhash / utils.phash          │
    │                            │ （保持输出 64bit 0/1 字符串）      │
    │ 改场景检测灵敏度逻辑       │ threshold_detector.ThresholdDetector│
    │ 改抽帧规则（如加首尾帧）   │ frame_extractor._plan_fixed_targets│
    │                            │ / scan_video_fingerprints          │
    │ 支持新视频格式             │ config.SUPPORTED_EXTENSIONS        │
    │ 加报告列                   │ report_generator.CSV_HEADERS +     │
    │                            │ _build_row 对应字段；              │
    │                            │ gui._setup_tree_columns 可选宽度   │
    │ 改分组报告版式             │ report_generator.write_xlsx_report │
    │                            │ （_write_block / _group_sheet_row）│
    │ 改日志轮转大小/份数        │ config.LOG_MAX_BYTES /             │
    │                            │ LOG_BACKUP_COUNT                   │
    │ 改损坏视频处理策略         │ frame_extractor.read_video_metadata│
    │                            │ （decode_error 标记）+             │
    │                            │ decision_maker._corrupt_decision   │
    │ 改删除行为（如只标记不删） │ decision_maker.apply_deletion      │
    │ 改 GUI 布局/交互           │ gui.py（业务调用仍走 run_pipeline）│
    │ 调快通道判据/采样块大小    │ fast_dedup.py +                    │
    │                            │ config.SAMPLE_HASH_BLOCK_SIZE      │
    │ 改副本件保留策略（谁留谁删）│ fast_dedup._pick_survivor          │
    │ 调指纹复用条件（功能 2）   │ fast_dedup.find_content_reuse      │
    │ 关闭快通道                 │ config.FAST_DEDUP_ENABLED 或       │
    │                            │ CLI --no-fast-dedup / GUI 开关     │
    │ 改人工改判逻辑             │ report_generator.load_review_      │
    │                            │ overrides + decision_maker.        │
    │                            │ apply_review_overrides             │
    │ 改分组策略（如加码率分组） │ metadata_processor +               │
    │                            │ main.collect_candidate_pairs 调用  │
    │ 加新的流水线阶段           │ main.run_pipeline 按 10 步模式插入，│
    │                            │ 记得走缓存短路 + cancel_check      │
    └────────────────────────────┴────────────────────────────────────┘

D.3 数据流（变量在各阶段的形态）
    List[str] 路径列表（排序）
      → Dict[path, VideoMetadata] 元数据字典（阶段②产出/缓存）
      → Dict[path, str] 采样指纹（阶段③，fast_dedup.compute_sample_hashes）
      → FastPathResult（阶段④：clusters / alias_map / forced_deletes /
        fast_pairs —— 字节级簇、孪生别名、预判删除、合成比对）
      → duration_groups / size_groups: Dict[组名或大小, List[path]]
      → candidate_pairs / frame_pairs: List[(path_a, path_b)]
        （frame_pairs = 候选对剔除簇内对）
      → Dict[path, VideoFingerprint]（fixed_frames + scene_frames，
        每帧含 frame_index / timestamp_sec / dhash / phash；
        subtitle_score 字幕得分（>10 分钟视频，None=未计算）；
        孪生件的指纹是簇代表的复制，仅 video_path 不同）
      → List[PairComparison]（phash/dhash/final 相似度 + is_duplicate
        + is_suspect 疑似标记 + is_file_level 标记快判来源）
      → groups: List[List[path]]（并查集聚合的重复组）
      → List[RetentionDecision]（每视频一行：分数 + action[KEEP/DELETE/
        UNIQUE/SUSPECT/CORRUPT] + decision_source 来源 + reason）
      → CSV 报告 + Excel 双 sheet 报告（report_generator）/ 删除执行
        （apply_deletion，只处理 action=DELETE）
    查任何阶段的字段含义，优先读 models.py 的 dataclass 注释。

D.4 扩展点建议（设计上已预留的位置）
    - 音频指纹：在 frame_extractor 的单遍扫描里顺带抽音频波形，
      指纹结构扩展 VideoFingerprint 即可，缓存表加列。
    - 更大规模（>1万）：把 ⑤ 比对阶段改分布式（按组拆分任务），
      缓存层换 PostgreSQL；①-④ 仍可保持单机顺序读。
    - Web 界面：gui.py 已把"操作+日志+结果"分离，换成 FastAPI + 前端
      时复用 run_pipeline 与 CSV 报告即可。
    - 学习式评分：decision_maker 的四维分数已分项输出到报告，
      可作为特征接入权重学习，不必改流水线骨架。

D.5 测试建议（本次交付已验证的用例，回归时可复用）
    - 构造测试视频：用 cv2.VideoWriter 写合成片（移动图形 + 硬切转场），
      用 shutil.copyfile 做完全拷贝、用亮度+3 重编码做近似重复。
    - 帧级断言点：完全拷贝与近似重复进同一组且各保留 1 个；
      不同内容同时长视频判唯一；短视频对（≤30s）判重正确；
      dry-run 后磁盘无变化；--execute + --trash-dir 后源目录只剩保留件
      且回收目录含被删件；二次运行日志显示"缓存命中 N 个、新解码 0 个"。
    - 快通道断言点（增量场景，共用一个缓存库连跑多轮）：
        复制件入库 → 待删除 + 判定来源=文件级快速判重 + 日志"新解码 0 个"；
        移动件（换路径重现）→ 复用历史指纹（日志"内容一致，复用历史指纹"）
        且判唯一（不误判重复）；
        两个全新相同文件 → 留一删一、副本"孪生别名"零解码、代表只解码 1 次；
        --no-fast-dedup → 同样判出重复但来源=帧级比对；
        旧 schema 库打开自动补列；
        --reviewed-report 填「人工复核」列 → 改判生效且依据留痕。
    - v4 断言点：构造汉明距离可控的假指纹单测疑似档（87.5 分→疑似、
      100 分→重复、84.4 分→双非）；垃圾字节 .mp4 → 报告「无法解码」
      且流水线不崩溃；xlsx 含「明细报告/分组报告」双 sheet、组内保留行
      在删除行之前、依据列非空；小 max_bytes 跑日志产生 .log.1 归档。
    - v5 断言点：PRAGMA journal_mode 返回 wal 且写后生成 -wal 文件；
      烧字幕帧（putText 在底部）的硬字幕得分显著高于纯色帧；
      tx3g 字节标记命中软字幕检测；五维权重公式 = 四维×0.85+字幕×0.15、
      有字幕版同条件总分更高、无字幕数据退回四维；不同 MD5 读块大小
      结果一致；单/多线程（--multithread）处置决定完全一致；
      SUBTITLE_CHECK_MIN_SEC=0 强制计算时重复组成员字幕得分已填充。
    - GUI 冒烟：python3 gui.py --demo（在 xvfb-run 下可无头自测）。

D.6 反模式（不要做的事）
    - 不要在 NAS 机械盘场景开多线程解码（SSD 场景的 --multithread 是
      有意的可选优化，但必须走 run_compute_jobs、worker 不碰库，见 C.2）。
    - 不要绕过缓存直接重复解码（性能设计的核心就是缓存短路）。
    - 不要把 dry-run 设计成默认执行（安全红线，见 C.1）。
    - 不要让 GUI 自己实现业务逻辑（必须复用 main.run_pipeline，
      否则命令行与界面行为会漂移）。
    - 不要在指纹计算里保留整段视频帧（长视频内存会爆；场景检测
      必须保持流式滑窗设计）。
    - 不要把字幕得分当作精确识别（它是启发式，只配 15% 权重，
      不得拿它单独做删留决定）。


================================================================
3. 术语表
================================================================
    dHash      差异哈希（相邻像素大小关系 → 64bit），抗亮度变化
    pHash      感知哈希（DCT 低频 → 64bit），抗缩放/压缩
    汉明距离   两个等长比特串不同位的个数
    初筛/精筛  pHash 快速排除 / dHash 精细确认，两级串行
    混合抽帧   固定间隔帧 + 场景切换关键帧的组合策略
    并查集     合并传递重复关系（A-B、B-C → {A,B,C}）的数据结构
    dry-run    试运行：只出报告不删文件（默认模式）
    mtime      文件修改时间，本系统中作为「入库时间」的替代
    采样指纹   头/中/尾各 4KB + 文件大小的 MD5，快通道粗筛钥匙（近乎免费）
    全文件 MD5 整文件字节的 MD5（顺序读不解码），快通道终审判据
    文件级快速判重  全量 MD5 一致即判重复（未经帧级比对），增量快通道
    指纹复用   同名同大小同采样指纹的历史指纹直接搬用（移动/复制件零解码）
    孪生别名   字节级重复簇的副本件共用簇代表指纹，不重复解码
    疑似线/疑似重复  融合相似度落在疑似线与判重线之间的对；不自动删，
                     报告单列交人工判断（SUSPECT）
    无法解码   损坏或格式不支持的视频（decode_error 非空）；单列报告，
               不参与比对、永不自动删（CORRUPT）
    分组报告   xlsx 的第二个 sheet：按重复组排布保留/删除行与依据
    字幕得分   0-100 启发式分（软字幕轨=100 / 硬字幕=底部边缘密度），
               >10 分钟视频的保留评分第五维（权重 15%）
    软字幕     独立字幕轨（容器内 tx3g/S_TEXT/ 等），播放器可开关
    硬字幕     烧录进画面的字幕（像素已混合），靠底部条带边缘密度识别
    WAL        SQLite 预写式日志模式：崩溃后自动恢复，缓存库不损坏
    run_compute_jobs  统一的顺序/线程池任务执行器（多线程开关的落点）
================================================================
