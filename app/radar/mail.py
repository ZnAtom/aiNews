#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B7：SoS 邮件发送——单发一封 SoS 综合邮件，与 Horizon 日报邮件共用同一套邮箱配置。

配置来源：
- app/Horizon/data/config.json 的 email 段（SMTP 地址/端口/账号/发件人名）
- Horizon .env 的密码变量（config.password_env，默认 EMAIL_PASSWORD）
- app/Horizon/data/subscribers.json 订阅者列表

邮件正文为 Markdown：纯文本 + HTML 双版本；顶部附网页访问横幅（B10 同款，
只含访客口令——邮件可能被转发）。当日若启用过备用 API（B16），顶部会再加
一条提示，说明主 API 限额/不可用、部分内容由备用端点生成。
"""

import html
import json
import os
import smtplib
import socket
import ssl
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

try:
    import markdown as _markdown
except ImportError:
    _markdown = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HORIZON_DATA = PROJECT_ROOT / "app" / "Horizon" / "data"
CONFIG_PATH = HORIZON_DATA / "config.json"
SUBSCRIBERS_PATH = HORIZON_DATA / "subscribers.json"
# B16 备用 API 启用标记（sos_daily.py / Horizon backup.py 写入，本文件只读）
FALLBACK_FLAG = HORIZON_DATA / "llm_fallback.json"


def _web_access_lines(env: dict):
    """B10 同款网页横幅。端口取 RADAR_WEB_PORT（默认 8080），访客口令取
    RADAR_GUEST_KEY（Horizon .env 已包含，sos_daily.load_env 会一并载入）。
    取不到局域网 IP 时返回 None。"""
    port = env.get("RADAR_WEB_PORT", "8080")
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass
    if not ip or ip.startswith("127."):
        return None
    url = f"http://{ip}:{port}"
    guest = env.get("RADAR_GUEST_KEY", "")
    tail = f"（访客口令 {guest}，校园网内可访问）" if guest else "（校园网内可访问）"
    plain = f"🌐 网页版：{url}{tail}"
    md = f"🌐 **网页版**：[{url}]({url}){tail}"
    return plain, md


def _fallback_notice_lines():
    """B16：当日启用过备用 API 时返回（纯文本行, markdown 行），否则 None。

    自包含实现（不能 import sos_daily——会循环 import）：只读标记文件。
    """
    try:
        data = json.loads(FALLBACK_FLAG.read_text(encoding="utf-8"))
        if (
            isinstance(data, dict)
            and data.get("date") == time.strftime("%Y-%m-%d")
            and data.get("count")
        ):
            count = data.get("count", 0)
            err = (data.get("last_error") or "").strip() or "未知错误"
            reason = f"主 API 限额或不可用，最近错误：{err}"
            plain = (
                f"⚠️ 今日部分内容由备用 API 生成（{reason}，共 {count} 次）。"
                "服务未中断，请知悉并尽快检查主 API 额度或状态。"
            )
            md = (
                f"⚠️ **今日部分内容由备用 API 生成**（{reason}，共 {count} 次）。"
                "服务未中断，请知悉并尽快检查主 API 额度或状态。"
            )
            return plain, md
    except Exception:
        pass
    return None


def _load_email_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    email_cfg = cfg.get("email") or {}
    if not email_cfg.get("enabled"):
        raise RuntimeError("config.json 的 email.enabled 为 false，邮件功能未开启")
    return email_cfg


def _load_subscribers() -> list:
    if not SUBSCRIBERS_PATH.exists():
        return []
    data = json.loads(SUBSCRIBERS_PATH.read_text(encoding="utf-8"))
    return [a for a in (data if isinstance(data, list) else []) if "@" in str(a)]


def send_markdown_mail(env: dict, subject: str, md_text: str, include_fallback_notice: bool = True) -> int:
    """把一份 Markdown 正文发给全部订阅者，返回发送成功的人数。失败抛异常。

    B16：include_fallback_notice=True 时，若当日启用过备用 API，正文顶部
    注入提示（放在网页横幅之前）；告警类邮件传 False 不注入，避免混淆。
    """
    email_cfg = _load_email_config()
    subscribers = _load_subscribers()
    if not subscribers:
        raise RuntimeError("订阅者列表为空（data/subscribers.json）")

    password_env = email_cfg.get("password_env", "EMAIL_PASSWORD")
    password = env.get(password_env, "") or os.environ.get(password_env, "")
    if not password:
        raise RuntimeError(f"环境变量 {password_env} 未设置，无法登录 SMTP")

    sender_name = email_cfg.get("sender_name", "AI 资讯雷达")
    from_addr = email_cfg.get("email_address", "")
    username = email_cfg.get("smtp_username") or from_addr
    host = email_cfg.get("smtp_server", "smtp.qq.com")
    port = int(email_cfg.get("smtp_port", 465))

    # B16：顶部备用 API 提示（在网页横幅之前）
    plain_body, md_body = md_text, md_text
    if include_fallback_notice:
        notice = _fallback_notice_lines()
        if notice:
            plain_body = f"{notice[0]}\n\n{plain_body}"
            md_body = f"{notice[1]}\n\n{md_body}"
    # 顶部网页横幅
    access = _web_access_lines(env)
    if access:
        plain_body = f"{access[0]}\n\n{plain_body}"
        md_body = f"{access[1]}\n\n{md_body}"

    safe_md = html.escape(md_body)
    html_content = _markdown.markdown(safe_md) if _markdown else f"<pre>{safe_md}</pre>"
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8">
    <style>
        body {{ font-family: sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }}
        h1, h2, h3 {{ color: #2c3e50; }}
        code {{ background-color: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-family: monospace; }}
        pre {{ background-color: #f4f4f4; padding: 15px; border-radius: 5px; overflow-x: auto; }}
        blockquote {{ border-left: 4px solid #ddd; padding-left: 15px; color: #777; }}
        .footer {{ margin-top: 40px; font-size: 12px; color: #888; text-align: center; border-top: 1px solid #eee; padding-top: 20px; }}
    </style></head>
    <body>{html_content}
        <div class="footer"><p>Sent by {html.escape(sender_name)}</p></div>
    </body></html>
    """

    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=context)
    else:
        server = smtplib.SMTP(host, port)
    try:
        server.ehlo()
        if port != 465:
            server.starttls(context=context)
            server.ehlo()
        server.login(username, password)
        sent = 0
        for subscriber in subscribers:
            msg = MIMEMultipart("alternative")
            # QQ 等服务商要求非 ASCII 发件人/主题遵循 RFC2047 编码
            msg["Subject"] = str(Header(subject, "utf-8"))
            msg["From"] = formataddr((str(Header(sender_name, "utf-8")), from_addr))
            msg["To"] = subscriber
            msg.attach(MIMEText(plain_body, "plain"))
            msg.attach(MIMEText(html_body, "html"))
            try:
                server.send_message(msg)
                sent += 1
            except Exception as exc:
                print(f"[邮件] 发送给 {subscriber} 失败：{type(exc).__name__}: {exc}")
        return sent
    finally:
        try:
            server.quit()
        except Exception:
            pass
