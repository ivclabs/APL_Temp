"""Email alerts through AWS SNS.

The SNS topic (created by Terraform) sends every message to the subscribed email address.
Turned off when ALERT_TOPIC_ARN is not set (local testing).
"""
import logging
import os
import sqlite3
from datetime import datetime

import boto3

log = logging.getLogger("apl_temp.notify")

TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")
REGION = os.environ.get("AWS_REGION", "eu-north-1")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
DAILY_LIMIT = int(os.environ.get("EMAIL_DAILY_LIMIT", 20))  # stops an inbox flood if the temp keeps flapping

_sns = None


def enabled():
    return bool(TOPIC_ARN)


def _client():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns", region_name=REGION)  # credentials come from the EC2 IAM role
    return _sns


def init_table(db_path):
    with sqlite3.connect(db_path) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS email_usage (
                   day   TEXT PRIMARY KEY,
                   sent  INTEGER NOT NULL DEFAULT 0
               )"""
        )


def _take_slot(db_path, day):
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO email_usage (day) VALUES (?) ON CONFLICT(day) DO NOTHING", (day,))
        cur = db.execute(
            "UPDATE email_usage SET sent = sent + 1 WHERE day = ? AND sent < ?", (day, DAILY_LIMIT)
        )
        return cur.rowcount == 1


def build_email(alarms, temp_c, when_local):
    """alarms: list of dicts with kind, message and (optional) ai_message."""
    kinds = " + ".join(a["kind"].upper() for a in alarms)
    subject = f"APL_Temp alarm: {kinds} – {temp_c:.1f} °C"

    lines = [f"Alarm at {when_local}", ""]
    for a in alarms:
        lines.append(f"{a['kind'].upper()}: {a['message']}")
        if a.get("ai_message"):
            lines.append(f"AI explanation: {a['ai_message']}")
        lines.append("")
    if PUBLIC_URL:
        lines.append(f"Live dashboard: {PUBLIC_URL}")
    lines.append("You get at most one email per alarm type every 15 minutes.")
    return subject[:100], "\n".join(lines)  # SNS email subjects can be at most 100 characters


def send_alarm_email(db_path, alarms, temp_c, when: datetime):
    """Send one email for all alarms from the same reading. Returns True if sent."""
    if not enabled() or not alarms:
        return False
    if not _take_slot(db_path, when.date().isoformat()):
        log.warning("Email daily limit (%d) reached - not sending", DAILY_LIMIT)
        return False

    subject, body = build_email(alarms, temp_c, when.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        _client().publish(TopicArn=TOPIC_ARN, Subject=subject, Message=body)
        log.info("Alarm email sent: %s", subject)
        return True
    except Exception:
        log.exception("Could not send alarm email")
        return False
