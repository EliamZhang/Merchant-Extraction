# Business bd — 商户知识库数据处理工具集

对澳大利亚商户数据进行解析、过滤、合并、关键词清洗、分类标注和 AI 验证的工具集。

## 目录结构

```
Business bd/
├── common/                  # 共享模块
│   ├── config.py                       # 全局配置
│   └── utils.py                        # 通用工具
├── scripts/                # 所有脚本（5 个）
│   ├── build_kb.py                    # 官方企业库构建（解析+过滤+合并+分类）
│   ├── clean_keywords.py              # 关键词清洗
│   ├── merge_add.py                   # 手工补充合并
│   ├── merchant_classifier.py         # AI 分类（DeepSeek）
│   └── verify_third_party_merchants.py # AI 第三方验证（DeepSeek）
├── xml2csv.sh               # 官方企业库合并（ABR XML → KB）
├── clean_keywords.sh        # 知识库关键词清洗
├── merge_add.sh             # 手工补充合并
├── run_full_pipeline.sh     # AI 企业库合并（分类→验证→分类）
├── run_with_retry.sh        # 失败自动重试包装器
├── raw/                    # 输入：ABR XML 报文
├── add/                    # 输入：手工补充 CSV
├── data/                   # 中间产物（parsed/, filtered.csv, kb_internal.csv）
├── cache/                  # API 调用缓存
├── output/                 # 验证输出
├── tests/                  # 测试
└── backup/                 # 历史脚本存档
```

## 两个主要工作流

### 工作流 A：ABR 报文 → 知识库

从澳大利亚商业登记 (ABR) 的 XML 报文中提取商户信息，经过过滤、合并、清洗和分类，生成结构化的商户知识库。

```
raw/*.xml  →  build_kb  →  clean_keywords  →  export  →  merchant_kb.csv
              (解析+过滤     (关键词清洗)       (5列投影
               合并+分类)                       排除GONE)
```

| 步骤 | 脚本 | 功能 |
|------|------|------|
| 1 | `build_kb.py` | 解析 XML → 过滤 → 增量合并 → 规则分类，输出 `data/kb_internal.csv` |
| 2 | `clean_keywords.py` | 清洗 keywords：移除过短词、停用词、去重 |
| 3 | `export_final()` | `kb_internal.csv` → `merchant_kb.csv`（5 列，排除 GONE） |
| — | `merge_add.py` | 独立通道：将 `add/*.csv` 手工合并到知识库 |

**一键执行：**

```bash
bash xml2csv.sh              # 增量模式（日常更新）
bash xml2csv.sh --full       # 全量关键词清洗
bash xml2csv.sh --skip-parse # 跳过 XML 解析
```

### 工作流 B：AI 处理

调用 DeepSeek API 进行智能分类和第三方验证。

| 脚本 | 功能 |
|------|------|
| `merchant_classifier.py` | 用 DeepSeek 对 `merchant_kb.csv` 中的商户进行 AI 分类 |
| `verify_third_party_merchants.py` | 验证银行交易对手方是否为真实商户，提取标准化名称和关键词 |

## 四个操作入口

所有操作都通过根目录的 Shell 脚本一键执行：

| 脚本 | 功能 | 用法 |
|------|------|------|
| `xml2csv.sh` | 官方企业库合并 | `bash xml2csv.sh` |
| `clean_keywords.sh` | 知识库关键词清洗 | `bash clean_keywords.sh` |
| `merge_add.sh` | 手工补充合并 | `bash merge_add.sh` |
| `run_full_pipeline.sh` | AI 企业库合并 | `bash run_full_pipeline.sh` |

```bash
# 官方企业库：ABR XML → 商户知识库
bash xml2csv.sh              # 增量
bash xml2csv.sh --full       # 全量清洗
bash xml2csv.sh --skip-parse # 跳过 XML 解析

# 知识库清洗：对 merchant_kb.csv 直接清洗
bash clean_keywords.sh
bash clean_keywords.sh --full

# 手工补充：将 add/*.csv 合并到知识库
bash merge_add.sh

# AI 企业库：分类 → 第三方验证 → 分类
export DEEPSEEK_API_KEY="sk-..."
bash run_full_pipeline.sh
```

## 各脚本用法

### build_kb — 官方企业库构建

```bash
python scripts/build_kb.py              # 全流程（解析→过滤→合并→分类）
python scripts/build_kb.py --skip-parse # 跳过 XML 解析
```

一条命令完成：解析 `raw/*.xml` → 过滤（PRV/PUB，排除 2023 年前注销）→ 增量合并 → 规则分类（24 个行业类别），输出 `data/kb_internal.csv`。后续由 `xml2csv.sh` 统一衔接清洗和导出。

### clean_keywords — 关键词清洗

```bash
# 快捷方式：直接清洗 merchant_kb.csv
bash clean_keywords.sh                    # 增量清洗
bash clean_keywords.sh --full             # 全量清洗
bash clean_keywords.sh --full --report cleaning_report.csv

# 或者直接调用 Python 脚本（支持任意文件）
python scripts/clean_keywords.py
python scripts/clean_keywords.py --full
python scripts/clean_keywords.py --input data/myfile.csv --report cleaning_report.csv
```

清洗规则：
1. 长度 `< 5` 的关键词 → 移除
2. 单个 token 且命中停用词（城市名、商业通用词、方位词等）→ 移除
3. 大小写去重

停用词集合定义在 `common/config.py`，包含 200+ 澳洲城市/区名、商业通用词、支付通道词等。

### merge_add — 手工补充合并

```bash
# 快捷方式
bash merge_add.sh
bash merge_add.sh --add-dir my_files/ --target my_kb.csv

# 或者直接调用 Python 脚本
python scripts/merge_add.py --add-dir add/ --target merchant_kb.csv
```

将 `add/` 目录下的手工维护 CSV 合并到目标知识库：
- 按商户名称（大小写和空格不敏感）匹配
- 已有商户：填补空白字段（keywords、link、category）
- 新商户：插入到文件顶部

> 合并后建议跑一次清洗：`bash clean_keywords.sh`

### merchant_classifier — AI 分类

```bash
python scripts/merchant_classifier.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --merchant-kb merchant_kb.csv \
  --cache cache/merchant_category_cache.json

# 常用选项
python scripts/merchant_classifier.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --batch-size 50 \           # 每批商户数（默认 50）
  --row-limit 100 \           # 只处理前 N 行（测试用）
  --include-existing \        # 重新分类已有类别的商户
  --dry-run-stats \           # 只统计，不调用 API
  --timeout-seconds 120 \     # API 超时（默认 120s）
  --max-retries 5             # 最大重试次数
```

调用 DeepSeek API 对 `merchant_kb.csv` 中 `category` 为空的商户进行分类。支持缓存（避免重复调用）和断点续传（`atexit` 保存）。

### verify_third_party_merchants — AI 第三方验证

```bash
python scripts/verify_third_party_merchants.py --api-key "$DEEPSEEK_API_KEY"

# 常用选项
python scripts/verify_third_party_merchants.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --input sample.csv \        # 输入文件（默认 sample.csv）
  --output output/verified.csv \
  --batch-size 6 \            # 每批候选数（默认 6，太大容易超时）
  --row-limit 100 \           # 只处理前 N 行（测试用）
  --skip-merchant-kb-update \ # 不更新 merchant_kb.csv
  --max-api-calls 50          # 限制 API 调用次数
```

使用 DeepSeek API 验证银行交易记录中的对手方是否为真实商户，提取标准化名称、关键词和验证链接。采用多层匹配策略：知识库 → 缓存 → AI API。

## 配置

所有可调参数集中在 `common/config.py`：

| 配置项 | 说明 |
|--------|------|
| `RAW_DIR` / `DATA_DIR` / `ADD_DIR` | 输入输出目录 |
| `KEEP_ENTITY_TYPES` | 保留的实体类型（PRV、PUB） |
| `CANCEL_CUTOFF_DATE` | 注销日期阈值（2023-01-01） |
| `MATCH_KEY_LENGTH` | SHA256 截断长度 |
| `MIN_KEYWORD_LEN` | 最短关键词长度 |
| `STOPWORDS` | 停用词集合（200+ 词） |
| `KB_INTERNAL_COLUMNS` | 内部 CSV 列定义（12 列） |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | 必填 |
| `DEEPSEEK_BASE_URL` | API 地址 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 模型名称 | `deepseek-v4-pro` |
| `DEEPSEEK_THINKING_TYPE` | 思考模式 | `none` |
| `DEEPSEEK_REASONING_EFFORT` | 推理强度 | `none` |

## 运行测试

```bash
python -m pytest tests/ -v
```

## 注意事项

- 所有 CSV 和 JSON 数据文件在 `.gitignore` 中（文件太大）
- `merchant_kb.csv` 约 360 万行，测试时建议用 `--row-limit`
- `sample.csv` 约 4.8 万行
- 缓存文件对成本控制至关重要——DeepSeek API 调用不是免费的
- 所有写操作使用原子保存（写入 `.tmp` 文件后 `replace`）
- `atexit` 注册确保中断时也能保存进度
