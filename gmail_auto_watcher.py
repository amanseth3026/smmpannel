#!/usr/bin/env python3
import argparse
import email
import imaplib
import json
import os
import random
import re
import time
import urllib.request
from email.header import decode_header
from email.utils import parsedate_to_datetime


def log(msg: str):
    if os.getenv("PY_WATCHER_LOG", "true").lower() == "true":
        print(f"[gmail_watcher] {msg}", flush=True)


def decode_mime(value: str) -> str:
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(part)
    return "".join(out)


def extract_text(msg) -> str:
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                try:
                    chunks.append(part.get_payload(decode=True).decode(errors="ignore"))
                except Exception:
                    pass
    else:
        try:
            chunks.append(msg.get_payload(decode=True).decode(errors="ignore"))
        except Exception:
            pass
    return "\n".join(chunks)


def amount_variants(amount: float):
    s2 = f"{amount:.2f}"
    s1 = f"{amount:.1f}"
    s0 = str(int(round(amount)))
    return {s2, s1, s0, s2.rstrip("0").rstrip(".")}


def contains_amount(text: str, variants):
    t = text.lower()
    for v in variants:
        if not v:
            continue
        if v.lower() in t:
            return True
        if ("₹" + v).lower() in t:
            return True
        if f"₹{v}".lower() in t:
            return True
        if f"inr {v}".lower() in t:
            return True
        if f"rs {v}".lower() in t:
            return True
    return False


def extract_amounts(text: str):
    raw = text or ""
    out = []
    for m in re.finditer(r"(?:₹|INR|RS\.?|RS)?\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)", raw, re.IGNORECASE):
        try:
            out.append(round(float(m.group(1).replace(",", "")), 2))
        except Exception:
            pass
    return out


def exact_amount_match(text: str, expected: float):
    target = round(float(expected), 2)
    vals = extract_amounts(text)
    return any(v == target for v in vals)


def extract_txn_id(text: str) -> str:
    m = re.search(r"transaction\s*id\s*[:\-]?\s*([A-Z0-9]{8,})", text, re.IGNORECASE)
    return m.group(1) if m else ""


def post_confirm(confirm_url: str, payload: dict):
    req = urllib.request.Request(
        confirm_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        return resp.status, body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payment-id", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--amount", required=True, type=float)
    parser.add_argument("--expires-at", required=True, type=int)
    parser.add_argument(
        "--max-runtime-sec",
        required=False,
        type=int,
        default=int(os.getenv("PY_WATCHER_MAX_RUNTIME_SEC", "240")),
    )
    args = parser.parse_args()

    gmail_user = os.getenv("GMAIL_IMAP_USER", "").strip()
    gmail_pass = os.getenv("GMAIL_IMAP_APP_PASSWORD", "").strip()
    from_match = os.getenv("GMAIL_FROM_MATCH", "famapp.in,famapp").lower()
    interval = int(os.getenv("PY_WATCHER_INTERVAL_SEC", "8"))
    max_age_sec = int(os.getenv("GMAIL_MESSAGE_MAX_AGE_SEC", "30"))
    confirm_url = os.getenv(
        "AUTO_CONFIRM_URL",
        "http://localhost:8888/.netlify/functions/auto-payment-confirm",
    )
    confirm_secret = os.getenv("AUTO_PAYMENT_CONFIRM_SECRET", "")

    if not gmail_user or not gmail_pass or not confirm_secret:
        log("missing env config; watcher exit")
        return

    started_at_ms = int(time.time() * 1000)
    hard_deadline_ms = started_at_ms + max(30, int(args.max_runtime_sec)) * 1000
    effective_expires_at = hard_deadline_ms

    log(
        f"start paymentId={args.payment_id} user={args.username} amount={args.amount:.2f} "
        f"deadline={effective_expires_at}"
    )
    from_tokens = [x.strip() for x in from_match.split(",") if x.strip()]
    keywords = ["received", "credited", "famx account", "famapp"]
    amount_set = amount_variants(args.amount)
    consecutive_errors = 0
    while int(time.time() * 1000) < effective_expires_at:
        try:
            min_allowed_ts = int(time.time() * 1000) - (max_age_sec * 1000)
            min_allowed_ts = max(min_allowed_ts, started_at_ms - 5000)
            imap = imaplib.IMAP4_SSL("imap.gmail.com")
            imap.login(gmail_user, gmail_pass)
            imap.select("INBOX")
            status, data = imap.search(None, "ALL")
            if status != "OK":
                log("imap search failed")
                imap.logout()
                backoff = min(20, max(1, interval) + (consecutive_errors * 2))
                jitter = random.uniform(0, 1.25)
                time.sleep(backoff + jitter)
                continue

            ids = data[0].split()[-25:]
            ids.reverse()

            for mail_id in ids:
                st, msg_data = imap.fetch(mail_id, "(RFC822)")
                if st != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                from_val = decode_mime(msg.get("From", "")).lower()
                subject = decode_mime(msg.get("Subject", ""))
                body = extract_text(msg)
                full = f"{subject}\n{body}"
                low = full.lower()
                try:
                    msg_dt = parsedate_to_datetime(msg.get("Date", ""))
                    msg_ts = int(msg_dt.timestamp() * 1000)
                    if msg_ts < min_allowed_ts:
                        continue
                except Exception:
                    continue

                if from_tokens and not any(tok in from_val for tok in from_tokens):
                    continue
                if not any(k in low for k in keywords):
                    continue
                if not contains_amount(full, amount_set):
                    continue
                if not exact_amount_match(full, args.amount):
                    continue

                txn_id = extract_txn_id(full)
                payload = {
                    "paymentId": args.payment_id,
                    "secret": confirm_secret,
                    "txnId": txn_id,
                    "subject": subject[:180],
                    "snippet": body[:200],
                    "senderEmail": from_val[:180],
                }
                status_code, _ = post_confirm(confirm_url, payload)
                log(f"match found, confirm status={status_code}, txn={txn_id or '-'}")
                imap.logout()
                if status_code == 200:
                    log("confirm success; watcher exit")
                    return
                break

            imap.logout()
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log(f"error: {e}")

        backoff = min(20, max(1, interval) + (consecutive_errors * 2))
        jitter = random.uniform(0, 1.25)
        time.sleep(backoff + jitter)

    log("expired/timeout without match")


if __name__ == "__main__":
    main()
