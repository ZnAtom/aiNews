#!/usr/bin/env python3
"""feedback.db 定期备份（数据保护）。

调教数据（votes / 权重 / 兴趣画像 / 点击 / 归档索引 / classics）是全系统
唯一不可再生数据，此处做仓库外每日快照：

- 用 SQLite 在线 backup API 取一致性快照（webapp 正在写入也安全）
- 备份到仓库外目录 ~/Myfile/backups/，命名 feedback-YYYYMMDD.db
- 保留最近 KEEP 份，自动轮转清理

由 radar-backup.timer（每日 07:30）驱动；失败经 OnFailure 发告警邮件。
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "feedback.db"
BACKUP_DIR = Path.home() / "Myfile" / "backups"
KEEP = 7


def main() -> int:
    if not DB_PATH.exists():
        print(f"[backup] 源库不存在：{DB_PATH}", file=sys.stderr)
        return 1
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    dest = BACKUP_DIR / f"feedback-{stamp}.db"

    # 只读连接 + backup API：即使源库正被 webapp 写入也能取到一致快照
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()

    # 轮转：只保留最近 KEEP 份
    backups = sorted(BACKUP_DIR.glob("feedback-*.db"))
    removed = 0
    for old in backups[:-KEEP]:
        old.unlink()
        removed += 1
    size_kb = dest.stat().st_size // 1024
    print(f"[backup] {dest}（{size_kb} KB）完成，清理过期 {removed} 份，现存 {len(backups) - removed} 份")
    return 0


if __name__ == "__main__":
    sys.exit(main())
