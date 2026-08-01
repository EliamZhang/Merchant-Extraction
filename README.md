# Merchant Extraction — 澳大利亚商户知识库数据处理工具集

对澳大利亚商户数据进行解析、过滤、合并、关键词清洗、分类标注和 AI 验证的工具集。

## 目录结构

```
Merchant Extraction/
├── build_knowledge_base.py   # ABR XML → merchant_kb.csv（官方企业库构建）
├── dedup_keywords.py          # 关键词清洗去重
├── merge_manual_entries.py    # 手工补充合并
├── label_merchants.py         # AI 分类（DeepSeek，全量 KB）
├── verify_merchants.py        # AI 第三方验证（DeepSeek，交易对手方）
├── classify_batch.py          # 分批分类辅助（extract / merge / status）
├── split_uncategorized.py     # 从 KB 中提取未分类商户，拆分为 JSON 分片
├── update_category.py         # 将分类结果回写到 merchant_kb.csv
├── settings.py                # 全局配置（路径、过滤规则、停用词、缩写白名单）
├── utils.py                   # 通用工具函数
├── .claude/                   # Claude Code 配置（hooks / skills / settings）
├── xml_input/                 # 输入：ABR XML 报文
├── manual_entries/            # 输入：手工补充 CSV
├── cache/                     # API 调用缓存
├── output/                    # 验证输出
├── knowledge-base-split/      # 中间产物：未分类商户 JSON 分片
├── knowledge-base-classify/   # 中间产物：分类结果 JSON
├── knowledge-base-web-classify/ # 中间产物：Web 分类批次和累积输出
│   └── batches/               # 待合并的分类批次
└── historical_kb/             # 历史 KB 快照
```

## 三个主要工作流

### 工作流 A：ABR 报文 → 知识库

从澳大利亚商业登记 (ABR) 的 XML 报文中提取企业主体，直接补充到 `merchant_kb.csv`。

```
xml_input/*.xml  →  build_knowledge_base.py  →  merchant_kb.csv
```

| 步骤 | 脚本 | 功能 |
|------|------|------|
| 1 | `build_knowledge_base.py` | 解析 XML → 过滤 PRV/PUB → 合并到 `merchant_kb.csv`；已有主体只补 keywords，新主体追加 |
| 2 | `dedup_keywords.py` | 可选：清洗 keywords（移除过短词、停用词、去重） |
| — | `merge_manual_entries.py` | 独立通道：将 `manual_entries/*.csv` 手工合并到知识库 |

```bash
python build_knowledge_base.py
python dedup_keywords.py --input merchant_kb.csv --full
```

### 工作流 B：KB 分类

对 `merchant_kb.csv` 中 `category` 为空的商户进行 AI 分类。

```
merchant_kb.csv
    ↓ split_uncategorized.py
knowledge-base-split/*.json          (20 个分片)
    ↓ classify_batch.py extract      (逐个分片，每次取 N 条)
knowledge-base-web-classify/batches/ (分类批次)
    ↓ Claude Code skill 调用 DeepSeek（web search）
knowledge-base-classify/*.json       (分类结果)
    ↓ update_category.py
merchant_kb.csv                      (回写 category 列)
```

| 步骤 | 脚本 | 功能 |
|------|------|------|
| 1 | `split_uncategorized.py` | 将 `merchant_kb.csv` 中未分类商户提取为 20 个 JSON 分片 |
| 2 | `classify_batch.py extract` | 从分片中取出下一批未处理的商户，写入 batch JSON |
| 3 | Claude Code skill | 调用 DeepSeek + web search 对每批商户进行分类 |
| 4 | `classify_batch.py merge` | 将分类结果合并回分片 JSON，同时写入累积输出 |
| 5 | `update_category.py` | 将全部分类结果回写到 `merchant_kb.csv` |

```bash
# 拆分
python split_uncategorized.py

# 分批处理
python classify_batch.py extract knowledge-base-split/merchant_kb_part_01.json --count 50
python classify_batch.py merge knowledge-base-split/merchant_kb_part_01.json knowledge-base-web-classify/batches/merchant_kb_part_01_batch_001.json
python classify_batch.py status    # 查看所有分片进度

# 最终回写
python update_category.py
```

也可以用 `label_merchants.py` 直接对全量 KB 进行分类：

```bash
python label_merchants.py --api-key "$DEEPSEEK_API_KEY"
```

### 工作流 C：第三方验证

调用 DeepSeek API 验证银行交易记录中的对手方是否为真实商户。

```
sample.csv  →  verify_merchants.py  →  output/sample_verified.csv
                (知识库 → 缓存 → AI API 三层匹配)
```

```bash
python verify_merchants.py --api-key "$DEEPSEEK_API_KEY"
```

## 各脚本用法

### build_knowledge_base — 官方企业库构建

```bash
python build_knowledge_base.py
python build_knowledge_base.py --xml-dir xml_input --target merchant_kb.csv
```

解析 `xml_input/*.xml` → 过滤（PRV/PUB，排除 2023 年前注销）→ 直接合并到 `merchant_kb.csv`。

### dedup_keywords — 关键词清洗去重

```bash
python dedup_keywords.py --input merchant_kb.csv
python dedup_keywords.py --input merchant_kb.csv --full
python dedup_keywords.py --input merchant_kb.csv --changed-since 2026-07-28
python dedup_keywords.py --input merchant_kb.csv --report cleaning_report.csv
```

清洗规则：长度过短、单 token 停用词、大小写重复。

### merge_manual_entries — 手工补充合并

```bash
python merge_manual_entries.py --add-dir manual_entries/ --target merchant_kb.csv
```

按商户名称匹配，已有商户填补空白字段，新商户插入到文件顶部。

### label_merchants — AI 分类（全量）

```bash
python label_merchants.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --merchant-kb merchant_kb.csv \
  --cache cache/merchant_category_cache.json \
  --batch-size 50 \
  --row-limit 100              # 测试用
```

### verify_merchants — AI 第三方验证

```bash
python verify_merchants.py --api-key "$DEEPSEEK_API_KEY"

# 常用选项
python verify_merchants.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --input sample.csv \
  --output output/verified.csv \
  --batch-size 5 \
  --row-limit 100 \
  --skip-merchant-kb-update \
  --max-api-calls 50
```

三层匹配策略：知识库关键词匹配 → 缓存 → DeepSeek API（batch 模式，默认每批 5 条）。验证通过后自动将标准化名称和关键词写回 `merchant_kb.csv`。

### classify_batch — 分批分类辅助

```bash
python classify_batch.py status                              # 查看所有分片进度
python classify_batch.py status knowledge-base-split/merchant_kb_part_01.json
python classify_batch.py extract <split_file.json> --count 50
python classify_batch.py merge <split_file.json> <batch.json>
python classify_batch.py next-file                           # 找下一个有待处理的分片
```

### update_category — 分类结果回写

```bash
python update_category.py
```

将 `knowledge-base-classify/` 中的分类结果按 `merchant_name` 匹配，更新 `merchant_kb.csv` 的 `category` 和 `category_updated_at` 列。

## 配置

`settings.py` 中的关键配置：

| 配置项 | 说明 |
|--------|------|
| `KEEP_ENTITY_TYPES` | 保留的实体类型（PRV、PUB） |
| `CANCEL_CUTOFF_DATE` | 注销日期阈值（2023-01-01） |
| `MIN_KEYWORD_LEN` | 最短关键词长度（5） |
| `STOPWORDS` | 停用词集合（200+ 词：城市名、商业通用词、方位词等） |
| `KNOWN_ABBREVIATIONS` | 知名缩写白名单（BP、KFC、ALDI、BWS 等 40+ 品牌） |
| `PAYMENT_PREFIX_WORDS` | 支付渠道前缀词（APPLE、GOOGLE、PAYPAL 等） |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | 必填 |
| `DEEPSEEK_BASE_URL` | API 地址 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 模型名称 | `deepseek-v4-flash` |
| `DEEPSEEK_THINKING_TYPE` | 思考模式 | `none` |
| `DEEPSEEK_REASONING_EFFORT` | 推理强度 | `none` |

## 数据规模

| 文件 | 行数 | 说明 |
|------|------|------|
| `merchant_kb.csv` | ~256 万 | 商户知识库主文件 |
| `sample.csv` | ~5.9 万 | 银行交易对手方样本 |

## 注意事项

- 所有 CSV 和 JSON 数据文件在 `.gitignore` 中（文件太大）
- 测试时建议用 `--row-limit` 限制处理量
- 缓存文件对成本控制至关重要——DeepSeek API 调用不是免费的
- 所有写操作使用原子保存（写入 `.tmp` 文件后 `replace`）
- `atexit` 注册确保中断时也能保存进度
- Claude Code 配置在 `.claude/` 目录下，包含 skills、hooks 和项目设置
