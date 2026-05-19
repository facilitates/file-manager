"""数据库连接与配置"""
import pymysql
import redis as redis_lib
import json

DB_CONFIG = {
    "host": "localhost",
    "user": "fmuser",
    "password": "FM@2026#secure",
    "database": "filemanager",
    "charset": "utf8mb4",
    "autocommit": True,
}

# Redis 连接池
_redis_pool = redis_lib.ConnectionPool(
    host='localhost', port=6379, db=0, decode_responses=True
)
rds = redis_lib.Redis(connection_pool=_redis_pool)

# 豆包配置
DOUBAO_KEY = "ark-6e3d9b8f-4b25-4b4a-8b40-6c0e7f386bb5-31b66"
DOUBAO_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_MODEL = "ep-20260518223807-r9sqp"

CLASSIFY_PROMPT = """用1-2个中文字给这张图一个分类名，从以下选：
猫, 狗, 动物, 风景, 美食, 人物, 截图, 表情包, 动漫,
文档, 代码, 设计, 自拍, 萌宠, 植物, 建筑, 车

如果都不匹配，给一个2字内的分类名。
只回复1-2个字，不要标点不要解释。"""


def get_db():
    return pymysql.connect(**DB_CONFIG)


def invalidate_cache():
    """清除缓存"""
    keys = rds.keys("fm:folders:*")
    if keys:
        rds.delete(*keys)
    rds.delete("fm:root_count")


def get_folders_from_db(user_id=None):
    """获取文件夹列表（优先 Redis 缓存，按用户隔离）"""
    if user_id is None:
        return []
    cache_key = f"fm:folders:{user_id}"
    cached = rds.get(cache_key)
    if cached:
        return json.loads(cached)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT f.id, f.name, COUNT(fl.id) as cnt
        FROM folders f
        LEFT JOIN files fl ON fl.folder_id = f.id
        WHERE f.owner_id = %s
        GROUP BY f.id, f.name ORDER BY f.name
    """, (user_id,))
    folders = [{"id": r[0], "name": r[1], "count": r[2]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    rds.setex(cache_key, 30, json.dumps(folders, ensure_ascii=False))
    return folders
