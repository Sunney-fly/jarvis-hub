"""邮箱（IMAP 收件 + SMTP 发件）。同步库放线程池执行。"""
import asyncio
import json
from email.header import decode_header

from ...config import Settings
from ..registry import Tool


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        parts = decode_header(value)
        return "".join(
            p[0].decode(p[1] or "utf-8", "replace") if isinstance(p[0], bytes) else str(p[0])
            for p in parts
        )
    except Exception:
        return str(value)


def _imap_list(settings: Settings, limit: int) -> list[dict]:
    import email
    import imaplib

    conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        conn.login(settings.imap_user, settings.imap_pass)
        conn.select("INBOX")
        _typ, data = conn.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []
        ids = ids[-limit:]
        out = []
        for msg_id in reversed(ids):
            try:
                _typ, msg_data = conn.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                raw = msg_data[0][1].decode("utf-8", "replace") if msg_data and msg_data[0] else ""
                msg = email.message_from_string(raw)
                out.append(
                    {
                        "from": _decode_header(msg.get("From")),
                        "subject": _decode_header(msg.get("Subject")),
                        "date": msg.get("Date"),
                    }
                )
            except Exception:
                continue
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _smtp_send(settings: Settings, to: str, subject: str, body: str) -> None:
    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = settings.mail_from
    msg["To"] = to
    server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port)
    try:
        server.login(settings.smtp_user, settings.smtp_pass)
        server.sendmail(settings.mail_from, [to], msg.as_string())
    finally:
        server.quit()


def _mail_configured(settings: Settings) -> bool:
    return bool(settings.imap_host and settings.imap_user and settings.imap_pass)


async def _list_handler(args: dict, settings: Settings) -> str:
    if not _mail_configured(settings):
        return "未配置邮箱（IMAP_HOST/IMAP_USER/IMAP_PASS），无法查收邮件"
    try:
        limit = max(1, min(50, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    try:
        mails = await asyncio.to_thread(_imap_list, settings, limit)
    except Exception as e:
        return f"查收邮件失败：{e!r}"
    return json.dumps({"mails": mails}, ensure_ascii=False)


async def _send_handler(args: dict, settings: Settings) -> str:
    to = str(args.get("to") or "").strip()
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "").strip()
    if not (to and subject and body):
        return "缺少参数：to、subject、body 均必填"
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_pass):
        return "未配置发件邮箱（SMTP_HOST/SMTP_USER/SMTP_PASS）"
    try:
        await asyncio.to_thread(_smtp_send, settings, to, subject, body)
    except Exception as e:
        return f"发送邮件失败：{e!r}"
    return f"邮件已发送给 {to}"


def tools(settings: Settings) -> list[Tool]:
    return [
        Tool(
            name="list_recent_emails",
            description="查看最近 N 封（默认 10）邮件的发件人、主题、日期。",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "数量，默认 10，最大 50"}},
            },
            handler=lambda a: _list_handler(a, settings),
        ),
        Tool(
            name="send_email",
            description="发送邮件。收件人/主题/正文均必填。",
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "收件人邮箱"},
                    "subject": {"type": "string", "description": "主题"},
                    "body": {"type": "string", "description": "正文"},
                },
                "required": ["to", "subject", "body"],
            },
            handler=lambda a: _send_handler(a, settings),
        ),
    ]
