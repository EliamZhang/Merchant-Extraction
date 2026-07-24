# Business bd — 澳大利亚商户知识库增量更新 Pipeline

基于 [ABR (Australian Business Register) Bulk Extract](https://data.gov.au/data/dataset/abn-bulk-extract) 原始 XML 报文，筛选、清洗、维护澳大利亚商户知识库 `merchant_kb.csv`。

## 快速开始

```bash
# 首次运行（解析 raw/ 中所有 XML）
python pipeline.py

# 换新报文后的增量更新
python pipeline.py

# 全量重新清洗所有关键词
python pipeline.py --full-clean

# 跳过 XML 解析（已有 data/parsed/*.csv）
python pipeline.py --skip-parse

# 预览模式（只统计，不写入）
python pipeline.py --dry-run

# 只运行到指定阶段
python pipeline.py --step filter
```

## 目录结构

```
Business bd/
├── config.py               ← 全局配置（路径、过滤规则、STOPWORDS）
├── pipeline.py             ← 一键执行入口
├── stages/                 ← 处理阶段
│   ├── parse.py            ← Stage 1: XML → CSV
│   ├── filter.py           ← Stage 2: 过滤 & 名称处理
│   ├── merge_update.py     ← Stage 3: 增量合并
│   ├── clean_keywords.py   ← Stage 4: 关键词清洗
│   └── categorize.py       ← Stage 5: 分类标注
├── raw/                    ← 原始 XML 报文（每几个月替换）
├── data/                   ← 中间产物（自动生成）
│   ├── parsed/             ← parse 阶段输出
│   ├── filtered.csv        ← filter 阶段输出
│   ├── kb_internal.csv     ← 含元数据的内部 KB
│   └── changelog.csv       ← 增量变更日志
├── backup/                 ← 旧脚本存档
└── merchant_kb.csv         ← 最终产出（5 列，原格式）
```

## Pipeline 流程

```
raw/*.xml
  │
  ▼
[Stage 1] parse.py         XML → CSV（全字段，不过滤）
  │  data/parsed/*.csv
  ▼
[Stage 2] filter.py        只保留 PRV/PUB + 排除 2023 前注销 + 生成 merchant_name
  │  data/filtered.csv
  ▼
[Stage 3] merge_update.py  按 match_key 增量合并新旧数据
  │  data/kb_internal.csv + data/changelog.csv
  ▼
[Stage 4] clean_keywords.py 清洗关键词（默认增量，--full-clean 全量）
  │  data/kb_internal.csv（原地更新）
  ▼
[Stage 5] categorize.py    基于关键词规则分类
  │  data/kb_internal.csv（原地更新）
  ▼
[Export]                   投影 5 列 → merchant_kb.csv
```

## 过滤规则

配置在 `config.py`，换报文时如需调整规则只改这一个文件：

| 规则 | 参数 | 说明 |
|------|------|------|
| 实体类型 | `KEEP_ENTITY_TYPES = {"PRV", "PUB"}` | 只保留 Australian Private/Public Company |
| 注销日期 | `CANCEL_CUTOFF_DATE = "20230101"` | 2023-01-01 前注销的丢弃 |
| 关键词长度 | `MIN_KEYWORD_LEN = 5` | ≤4 字符的单 token 关键词被移除 |
| 停用词 | `STOPWORDS = {...}` | ~400 个澳洲地名/商业通用词/支付通道词 |

## 增量更新机制

每次运行时，用 `match_key`（法定主体名称 SHA256 哈希）匹配新旧记录：

- **match_key 已存在** → 更新元数据，keywords 变了就标记为需重新清洗
- **match_key 新的** → 新增记录，生成关键词并清洗
- **旧 KB 中有但新报文没有** → 标记为 `GONE`，不出现在最终 `merchant_kb.csv`

首次运行时 `data/kb_internal.csv` 不存在，全量初始化。

## merchant_kb.csv 格式

最终产出保持原始 5 列格式：

```
merchant_name, keywords, link, category, keyword_created_at
```

内部文件 `data/kb_internal.csv` 包含额外元数据列（match_key, entity_type, abn_status, status_date, state, record_updated, in_kb_since），仅用于增量匹配，不出现在最终 CSV 中。

## 配置参数

编辑 `config.py`：

```python
# 只保留这两种实体类型
KEEP_ENTITY_TYPES = frozenset({"PRV", "PUB"})

# 注销日期阈值
CANCEL_CUTOFF_DATE = "20230101"

# 最短关键词长度
MIN_KEYWORD_LEN = 5
```

## 依赖

- Python 3.10+
- 标准库 only（`xml.etree.ElementTree`, `csv`, `hashlib`, `argparse`, `pathlib`）
- 无需安装第三方包

## 换报文操作步骤

每几个月拿到新 XML 报文后：

1. 清空 `raw/` 目录
2. 放入新 XML 文件（保持 `Public*.xml` 命名）
3. 运行 `python pipeline.py`
4. 查看 `data/changelog.csv` 确认变更
5. 检查 `merchant_kb.csv` 最终产出
