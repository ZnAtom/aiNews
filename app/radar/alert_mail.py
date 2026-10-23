#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B14：systemd 服务失败告警邮件。

链路：horizon-daily / sos-daily / radar-distill 的 [Unit] 段挂
OnFailure=alert-mail@%n.service → alert-mail@.service 模板单元收到
失败单元名（%i）→ 调用本脚本：抓 journalctl 最近日志 + 相关日志文件
尾部，经 mail.send_markdown_mail 发给全部订阅者（与 SoS 邮件共用
config.json email 段 + subscribers.json）。

用法：alert_mail.py <失败单元名>
      单元名不存在于 systemd 时容错（journalctl 为空），可用于手动测试：
      ./.venv/bin/python alert_mail.py test-alert.service
"""

import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mail import send_markdown_mail
from sos_daily import ENV_PATH, load_env

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = PROJECT_ROOT / "out"

# 失败单元 → 额外追加的日志文件（尾部 30 行）
LOG_FILES = {
    "horizon-daily.service": OUT_DIR / "horizon-daily.log",
    "sos-daily.service": OUT_DIR / "sos-daily.log",
}


def _journal_tail(unit: str, lines: int = 40) -> str:
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager", "-q"],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout.strip() or "（journalctl 无该单元日志——可能是手动测试触发）"
    except Exception as e:  # noqa: BLE001
        return f"（journalctl 调用失败：{e}）"


def _file_tail(path: Path, lines: int = 30) -> str:
    try:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: alert_mail.py <失败单元名>")
        return 2
    unit = sys.argv[1]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    extra = ""
    if unit in LOG_FILES:
        tail = _file_tail(LOG_FILES[unit])
        if tail:
            extra = f"\n\n### 应用日志尾部（{LOG_FILES[unit].name}）\n\n```\n{tail}\n```"

    subject = f"🚨 AI 雷达告警：{unit} 失败 @ {now}"
    md = (
        f"# 服务失败告警\n\n"
        f"- **失败单元**：`{unit}`\n"
        f"- **时间**：{now}\n"
        f"- **主机**：{socket.gethostname()}\n\n"
        f"今天的日报/综合可能没有生成，网页将停留在旧日期；"
        f"修复配置后手动补跑：\n\n"
        f"```bash\n"
        f"cd app/Horizon && uv run horizon -l INFO   # 补日报\n"
        f"cd app/radar && ./.venv/bin/python sos_daily.py --wait-hours 0   # 补 SoS\n"
        f"```\n"
        f"{extra}\n\n"
        f"### journalctl 最近日志\n\n```\n{_journal_tail(unit)}\n```"
    )

    env = load_env(ENV_PATH)
    n = send_markdown_mail(env, subject, md)
    print(f"[alert-mail] {unit} 告警邮件已发送给 {n} 位订阅者")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
