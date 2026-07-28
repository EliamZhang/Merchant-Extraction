# Business bd — 商户知识库数据处理工具集

对澳大利亚商户数据进行解析、过滤、合并、关键词清洗、分类标注和 AI 验证的工具集。

## 目录结构

```
Business bd/
├── build_knowledge_base.py  # 官方企业库构建（XML 直接合并到 merchant_kb.csv）
├── clean_keywords.py        # 关键词清洗
├── merge_manual_entries.py  # 手工补充合并
├── classify_merchants.py    # AI 分类（DeepSeek）
├── verify_merchants.py      # AI 第三方验证（DeepSeek）
├── config.py                # 全局配置
├── utils.py                 # 通用工具
├── xml_input/               # 输入：ABR XML 报文
├── manual_entries/          # 输入：手工补充 CSV
├── cache/                   # API 调用缓存
├── output/                  # 验证输出
├── tests/                   # 测试
└── backup/                  # 历史脚本存档
```

## 两个主要工作流

### 工作流 A：ABR 报文 → 知识库

从澳大利亚商业登记 (ABR) 的 XML 报文中提取企业主体，直接补充到 `merchant_kb.csv`。

```
xml_input/*.xml  →  build_knowledge_base.py  →  merchant_kb.csv
                    (解析 + 过滤 + 去重 + 关键词补充)
```

| 步骤 | 脚本 | 功能 |
|------|------|------|
| 1 | `build_knowledge_base.py` | 解析 XML → 过滤 PRV/PUB → 合并到 `merchant_kb.csv`；已有主体只补 keywords，新主体追加 |
| 2 | `clean_keywords.py` | 可选：清洗 keywords：移除过短词、停用词、去重 |
| — | `merge_manual_entries.py` | 独立通道：将 `manual_entries/*.csv` 手工合并到知识库 |

```bash
python build_knowledge_base.py
python clean_keywords.py --input merchant_kb.csv --full
```

### 工作流 B：AI 处理

调用 DeepSeek API 进行智能分类和第三方验证。

| 脚本 | 功能 |
|------|------|
| `classify_merchants.py` | 用 DeepSeek 对 `merchant_kb.csv` 中的商户进行 AI 分类 |
| `verify_merchants.py` | 验证银行交易对手方是否为真实商户，提取标准化名称和关键词 |

## 各脚本用法

### build_knowledge_base — 官方企业库构建

```bash
python build_knowledge_base.py
python build_knowledge_base.py --xml-dir xml_input --target merchant_kb.csv
```

一条命令完成：解析 `xml_input/*.xml` → 过滤（PRV/PUB，排除 2023 年前注销）→ 直接合并到 `merchant_kb.csv`。如果主体已存在，不新增重复行，只把 ABR 里的别名/交易名补进 keywords。

### clean_keywords — 关键词清洗

```bash
python clean_keywords.py --input merchant_kb.csv
python clean_keywords.py --input merchant_kb.csv --full
python clean_keywords.py --input merchant_kb.csv --report cleaning_report.csv
```

清洗规则：
1. 长度 `< 5` 的关键词 → 移除
2. 单个 token 且命中停用词（城市名、商业通用词、方位词等）→ 移除
3. 大小写去重

### merge_manual_entries — 手工补充合并

```bash
python merge_manual_entries.py --add-dir manual_entries/ --target merchant_kb.csv
```

将 `manual_entries/` 目录下的手工维护 CSV 合并到目标知识库：
- 按商户名称（大小写和空格不敏感）匹配
- 已有商户：填补空白字段（keywords、link、category）
- 新商户：插入到文件顶部

> 合并后建议跑一次清洗：`python clean_keywords.py --input merchant_kb.csv`

### classify_merchants — AI 分类

```bash
python classify_merchants.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --merchant-kb merchant_kb.csv \
  --cache cache/merchant_category_cache.json

# 常用选项
python classify_merchants.py \
  --api-key "$DEEPSEEK_API_KEY" \
  --batch-size 50 \           # 每批商户数（默认 50）
  --row-limit 100 \           # 只处理前 N 行（测试用）
  --include-existing \        # 重新分类已有类别的商户
  --dry-run-stats \           # 只统计，不调用 API
  --timeout-seconds 120 \     # API 超时（默认 120s）
  --max-retries 5             # 最大重试次数
```

调用 DeepSeek API 对 `merchant_kb.csv` 中 `category` 为空的商户进行分类。支持缓存（避免重复调用）和断点续传（`atexit` 保存）。

### verify_merchants — AI 第三方验证

```bash
python verify_merchants.py --api-key "$DEEPSEEK_API_KEY"

# 常用选项
python verify_merchants.py \
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

所有可调参数集中在 `config.py`：

| 配置项 | 说明 |
|--------|------|
| `RAW_DIR` / `ADD_DIR` | 输入目录 |
| `KEEP_ENTITY_TYPES` | 保留的实体类型（PRV、PUB） |
| `CANCEL_CUTOFF_DATE` | 注销日期阈值（2023-01-01） |
| `MIN_KEYWORD_LEN` | 最短关键词长度 |
| `STOPWORDS` | 停用词集合（200+ 词） |

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
