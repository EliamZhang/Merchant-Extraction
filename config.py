"""
Business bd Pipeline — 全局配置
===============================
所有路径、过滤规则、关键词清洗参数集中管理。
每次换新报文只需修改此文件（如果规则变了），其余 pipeline 代码无需改动。
"""

from pathlib import Path

# ─── 目录 ─────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR / "raw"
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = BASE_DIR / "backup"

# ─── 中间产物 ─────────────────────────────────────────
PARSED_DIR = DATA_DIR / "parsed"           # parse.py 输出（每个 XML 一个 CSV）
FILTERED_FILE = DATA_DIR / "filtered.csv"  # filter.py 输出
INTERNAL_FILE = DATA_DIR / "kb_internal.csv"  # 含全部元数据的内部 KB
CHANGELOG_FILE = DATA_DIR / "changelog.csv"   # 增量变更日志

# ─── 最终产出 ─────────────────────────────────────────
FINAL_OUTPUT = BASE_DIR / "merchant_kb.csv"  # 5 列，原始格式

# ═══════════════════════════════════════════════════════
# 过滤规则（换报文时按需调整）
# ═══════════════════════════════════════════════════════

# 只保留这两种实体类型
KEEP_ENTITY_TYPES = frozenset({"PRV", "PUB"})
# PRV = Australian Private Company
# PUB = Australian Public Company

# 注销日期阈值：此日期**之前**注销的丢弃（格式 YYYYMMDD）
CANCEL_CUTOFF_DATE = "20230101"  # 2023-01-01

# 匹配键长度（SHA256 截断字符数）
MATCH_KEY_LENGTH = 16

# 内部标记：旧 KB 中有但新报文中消失的记录
STATUS_GONE = "GONE"

# ═══════════════════════════════════════════════════════
# 关键词清洗参数
# ═══════════════════════════════════════════════════════

MIN_KEYWORD_LEN = 5  # 最短关键词长度

# ─── 完整 STOPWORDS 集合 ───
STOPWORDS = {
    # ── 澳洲主要城市 ──
    "SYDNEY", "MELBOURNE", "PERTH", "BRISBANE", "ADELAIDE",
    "HOBART", "DARWIN", "CANBERRA", "GOLD COAST", "NEWCASTLE",

    # ── 常见区/镇名 ──
    "IPSWICH", "TOOWOOMBA", "CAIRNS", "BALLARAT", "BENDIGO",
    "ALBURY", "DUBBO", "ORANGE", "PENRITH", "CAMPBELLTOWN",
    "LIVERPOOL", "PARRAMATTA", "CHATSWOOD", "HURSTVILLE",
    "BANKSTOWN", "BLACKTOWN", "FAIRFIELD", "CABRAMATTA",
    "WOLLONGONG", "DAPTO", "CORRIMAL", "SHELLHARBOUR", "FIGTREE",
    "NOWRA", "BATEMANS", "BEGA", "COOMA", "GOULBURN",
    "MORUYA", "YASS", "COWRA", "FORBES", "PARKES",
    "BROKEN", "GRIFFITH", "LEETON", "NARRANDERA", "WAGGA",
    "WODONGA", "SHEPPARTON", "WANGARATTA", "BENALLA",
    "ECHUCA", "SWAN", "MILDURA", "HORSHAM", "ARARAT",
    "BAIRNSDALE", "SALE", "TRARALGON", "WARRAGUL", "MOE",
    "MORWELL", "DANDENONG", "FRANKSTON", "CRANBOURNE", "BERWICK",
    "PAKENHAM", "MORNINGTON", "ROSEDALE", "SUNBURY", "MELTON",
    "WERRIBEE", "GEELONG", "TORQUAY", "COLAC", "WARRNAMBOOL",
    "HAMILTON", "PORTLAND", "CASTLEMAINE", "KYNETON",
    "SUNSHINE", "BROADMEADOWS", "CRAIGIEBURN", "EPPING", "BUNDOORA",
    "HEIDELBERG", "DONCASTER", "RINGWOOD", "BOX", "BOROONDARA",
    "MOONEE", "ESSENDON", "BRUNSWICK", "COBURG", "PRESTON",
    "RESERVOIR", "THOMASTOWN", "LALOR", "JACANA", "GLENROY",
    "OAKLEIGH", "CLAYTON", "SPRINGVALE", "DINGLEY", "MORDIALLOC",
    "MENTONE", "SANDRINGHAM", "BRIGHTON", "ST", "ELSTERNWICK",
    "CAULFIELD", "MALVERN", "ARMADALE", "TOORAK", "PRAHRAN",
    "SOUTH", "PORT", "ALBERT", "FOOTSCRAY", "WILLIAMSTOWN",
    "ASCOT", "HENDRA", "CLAYFIELD", "ALBION",
    "LUTWYCHE", "CHERMSIDE", "ASPLEY", "ZILLMERE", "GEEBUNG",
    "STRATHPINE", "PETRIE", "KALLANGUR", "CABOOLTURE", "MORAYFIELD",
    "BURPENGARY", "DECEPTION", "NORTH", "REDCLIFFE", "MARGATE",
    "SCARBOROUGH", "WOODY", "CLONTARF", "SANDGATE", "BRACKEN",
    "BALD", "FITZGIBBON", "TAIGUM", "BOONDALL", "NUDGEE",
    "BANYO", "VIRGINIA", "NUNDAH", "TOOMBUL", "WAVELL",
    "KEDRON", "GORDON", "EVERTON", "MCDOWALL", "BRIDGEMAN",
    "ALBANY", "CANNING", "FREMANTLE", "JOONDALUP", "MANDURAH",
    "MIDLAND", "ROCKINGHAM", "GOSNELLS", "KALAMUNDA",
    "BELMONT", "VICTORIA", "KEWDALE", "CLOVERDALE",
    "BURSWOOD", "RIVERVALE", "MAYLANDS", "BASSENDEAN", "GUILDFORD",
    "MIDVALE", "ELLENBROOK", "AVON", "KALGOORLIE",
    "BUNBURY", "BUSSELTON", "GERALDTON", "CARNARVON",
    "BROOME", "KUNUNURRA", "EAST", "NORAM",
    "LAUNCESTON", "DEVONPORT", "BURNIE", "KINGSTON", "GLENORCHY",
    "CLAREMONT", "MOONAH", "LINDISFARNE", "HOWRAH", "SORELL",
    "NEW", "ROSETTA", "BERRIEDALE", "CHIGWELL", "MONTROSE",

    # ── 商业通用词 ──
    "REAL", "ESTATE", "PROPERTY", "FINANCIAL", "FINANCE",
    "LOAN", "LOANS", "HOME", "HOMES", "RENTAL",
    "RENTALS", "AGENCY", "AGENT", "INVESTMENT", "INVESTMENTS",
    "VENTURES", "ENTERPRISE", "ENTERPRISES", "TRADING",
    "HOLDING", "MANAGEMENT", "SOLUTIONS", "CONSULTING",
    "CONSULTANCY", "CONSULTANTS", "ASSOCIATES", "PARTNERS",
    "DISTRIBUTORS", "WHOLESALE", "SUPPLIES", "SUPPLIERS",

    # ── 方位/基础设施词 ──
    "WEST", "EAST", "NORTH", "SOUTH", "CENTRAL", "CITY",
    "TOWN", "VALLEY", "BAY", "BEACH", "PARK",
    "STREET", "ROAD", "STATION", "SQUARE", "CENTRE",
    "CENTER", "PLAZA", "MALL", "AIRPORT", "HARBOUR",
    "HARBOR", "BRIDGE", "HILL", "HILLS", "MOUNT",
    "LAKE", "RIVER", "COAST", "POINT", "CREEK",
    "ISLAND", "GARDEN", "GARDENS", "HEIGHTS",
    "JUNCTION", "CROSSING", "GATE", "GATES",
    "VILLAGE", "GROVE", "GLEN", "DALE",
    "WOOD", "WOODS", "FIELD", "FIELDS",
    "MEADOW", "MEADOWS", "GREEN", "SPRINGS",

    # ── 文档中提到的具体问题关键词 ──
    "PARKINSON", "VINCENTIA",
    "YAMBA", "ILUKA", "LIDCOMBE", "KIRWAN",
    "METRO", "STATE", "SYSTEMS", "SALES",
    "ELEVEN", "WEST", "SPRING",

    # ── 支付通道词/交易类型词 ──
    "CARD", "VISA", "EFTPOS", "BPAY", "OSKO",
    "PAYMENT", "PURCHASE", "TRANSFER", "DEPOSIT",
    "FEE", "INTEREST", "OVERDRAWN",
    "LIMITED", "GROUP", "HOLDINGS",
    "AUSTRALIA", "AUS",
}

# 归一化为大写
STOPWORDS = {w.upper() for w in STOPWORDS}

# ═══════════════════════════════════════════════════════
# 内部 CSV 列定义
# ═══════════════════════════════════════════════════════

KB_INTERNAL_COLUMNS = [
    "match_key",          # sha256(法定主体名称)[:16]
    "merchant_name",      # 清洗后的商户名
    "keywords",           # 匹配关键词
    "link",               # 链接
    "category",           # 分类
    "entity_type",        # PRV 或 PUB
    "abn_status",         # ACT / CAN / GONE
    "status_date",        # ABNStatusFromDate
    "state",              # BusinessAddress State
    "record_updated",     # recordLastUpdatedDate
    "keyword_created_at", # 关键词最近一次生成/变更时间
    "in_kb_since",        # 首次进入 KB 的时间
]

# 最终输出列（原始格式 5 列）
FINAL_OUTPUT_COLUMNS = [
    "merchant_name",
    "keywords",
    "link",
    "category",
    "keyword_created_at",
]
