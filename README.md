# CRISPRTE Species Extension — rn6 (大鼠)

把一个新物种接入 [CRISPRTE](https://github.com/WanluLiuLab/CRISPRTE) / [crisprte.cn](https://crisprte.cn) 的端到端流程脚本。本仓库以大鼠 **rn6** 为例,可作为接入任意新物种的模板。

> 上游工具:CRISPRTE(Liu Lab, ZJU-UoE)。
> 论文:https://link.springer.com/article/10.1186/s13100-024-00313-0
> 本仓库**不含上游源码本体**,只含「接入新物种」所需的补丁与流程脚本。

## 这是什么

CRISPRTE 是面向转座子(TE)的 CRISPR sgRNA 设计工具,原版支持 hg38(人)、mm10(小鼠)。本仓库记录把 **rn6** 接入的完整 7 阶段流程。

## 流程总览

| 阶段 | 脚本 | 作用 |
|---|---|---|
| 1 建注释底图 | `stage1_annotation/annotation_bed.py` | GTF + RepeatMasker → 五类区间注释 BED |
| 2 打补丁 | `stage2_patches/insert_load_fast.py`, `patch_trie2db.py` | 给上游 Trie 加 C 层 `load_fast`;Azimuth 评分分批防 OOM |
| 3 建库 | `stage3_build_db/build_rn6_db.py` | 抽全基因组 N20NGG → 建 Trie + table1/table2 |
| 3.5 评分续跑 | `scoring/safe_mp_resume.py` | 多进程续跑 table1 评分(替代 Step2,见下) |
| 4 脱靶扫描 | `stage4_offtarget/offtarget_mp.py` | Trie ≤3 错配近似搜索 → mm1/mm2/mm3 |
| 5 导 PostgreSQL | *(待写)* | SQLite → Postgres 库 `crisprtern6`,带类型转换 |
| 6 生成 refData | *(待写)* | 4 个前端参考文件 |
| 7 接入网站 | *(待写)* | `DB_MAP` 注册 + 前端 ga 选项 |

## 目录结构

```

.

├── stage1_annotation/   annotation_bed.py

├── stage2_patches/      insert_load_fast.py  patch_trie2db.py

├── stage3_build_db/     build_rn6_db.py

├── stage4_offtarget/    offtarget_mp.py

├── scoring/             safe_mp_resume.py        # table1 评分的 canonical 版

└── legacy/              fast_resume_scoring.py

resume_step2.py          # 上游整体跑法(会 OOM),仅参考

```

## 依赖

- Python 3.x:numpy、pandas、tqdm、joblib、pybedtools(需系统装 bedtools)、Azimuth(上游 `src/azimuth`)
- 上游 CRISPRTE 的 C 扩展 Trie(`PyExtensions/Trie`),**改 `.c` 后必须重编 `.so`**
- SQLite 3 / 后续 PostgreSQL

## 运行顺序

```

# 0. clone 上游并打补丁(改完务必重编 Trie 的 .so)

python stage2_patches/insert_load_fast.py

python stage2_patches/patch_trie2db.py

# 1. 建注释底图(需 rn6.fa.fai / *.gtf / rmsk.txt)

python stage1_annotation/annotation_bed.py

# 2. 建库(产出 rn6.crisprte.db + .trie.data)

python stage3_build_db/build_rn6_db.py

# 3. 评分续跑(多进程,可中断续传)

python scoring/safe_mp_resume.py

# 4. 脱靶扫描(回填 mm1/2/3,支持中断续传)

nohup python -u stage4_offtarget/offtarget_mp.py > offtarget_$(date +%Y%m%d_%H%M).log 2>&1 &

```

## 关于评分(重要)

`table1` 的最终评分由 **`scoring/safe_mp_resume.py`** 多进程续跑完成,**替代 `build_rn6_db.py` 内的 Step2 评分** —— 因为上游 `trie2db_2` 在 rn6 规模下整体评分会 OOM。该脚本关掉 BLAS/OMP 多线程爆炸、按 `COUNT(*) FROM table1` 续传、joblib 强制单线程跑 Azimuth、锁冲突重试。`legacy/` 下两个为开发迭代版,不维护。


## 当前状态

- ✅ 阶段 1–4 完成(rn6 脱靶扫描已收尾,约 234 万 TE 靶)
- 🚧 阶段 5–7 进行中(导库 / refData / 网站接入)

## 说明

- `CRISPRTE/` 上游本体不纳入本仓库,请从上游克隆后应用 `stage2_patches/`。
- `legacy/` 仅存档开发过程脚本,不保证可用。

