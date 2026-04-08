#!/usr/bin/env python3
import argparse
import datetime as dt
import email
import imaplib
import json
import os
import re
import time
import urllib.error
import urllib.request
from email.header import decode_header
from email.utils import parsedate_to_datetime


def log(msg):
    if os.getenv("PY_WATCHER_LOG", "false").lower() == "true":
        print(msg, flush=True)


def parse_args():
    ap = argparse.ArgumentParser(description="Gmail IMAP auto-payment watcher")
    ap.add_argument("--payment-id", required=True)
    ap.add_argument("--username", required=True)
    ap.add_argument("--amount", required=True)
    ap.add_argument("--expires-at", required=True)
    ap.add_argument("--max-runtime-sec", type=int, default=None)
    return ap.parse_args()


def decode_mime_words(value):
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            try:
                out.append(part.decode(enc or "utf-8", errors="ignore"))
            except Exception:
                out.append(part.decode("utf-8", errors="ignore"))
        else:
            out.append(str(part))
    return "".join(out)


def extract_text_from_message(msg):
    texts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype in ("text/plain", "text/html") and "attachment" not in disp:
                payload = part.get_payload(decode=True) or b""
                try:
                    text = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    text = payload.decode("utf-8", errors="ignore")
                if ctype == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                texts.append(text)
    else:
        payload = msg.get_payload(decode=True) or b""
        try:
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
        except Exception:
            text = payload.decode("utf-8", errors="ignore")
        texts.append(text)
    return "\n".join(texts)


def extract_amounts(text):
    raw = text or ""
    pattern = re.compile(
        r"(?:INR|RS\.?|RS|₹)?\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)",
        re.IGNORECASE,
    )
    out = []
    for m in pattern.findall(raw):
        try:
            n = float(m.replace(",", ""))
            out.append(round(n, 2))
        except Exception:
            continue
    return out


def amount_matches(text, amount):
    try:
        target = round(float(amount), 2)
    except Exception:
        return False
    for n in extract_amounts(text):
        if round(n, 2) == target:
            return True
    return False


def message_looks_like_credit(text):
    keywords = os.getenv(
        "GMAIL_CREDIT_KEYWORDS",
        "credited,received,success,payment,upi,money received",
    )
    tokens = [k.strip().lower() for k in keywords.split(",") if k.strip()]
    body = (text or "").lower()
    return any(t in body for t in tokens)


def extract_txn_id(text):
    raw = text or ""
    patterns = [
        r"transaction\s*id\s*[:\-]?\s*([A-Z0-9]{8,})",
        r"utr\s*[:\-]?\s*([A-Z0-9]{6,})",
        r"ref(?:erence)?\s*(?:no\.?|number)?\s*[:\-]?\s*([A-Z0-9]{6,})",
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            return re.sub(r"[^A-Z0-9]", "", m.group(1).upper())
    # fallback: first long alnum token
    m = re.search(r"\b([A-Z0-9]{10,})\b", raw, re.IGNORECASE)
    if m:
        return re.sub(r"[^A-Z0-9]", "", m.group(1).upper())
    return ""


def post_confirm(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            try:
                j = json.loads(body)
            except Exception:
                j = {}
            return resp.status, j, body
    except urllib.error.HTTPError as e:
        return e.code, {}, e.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return 0, {}, str(e)


def main():
    args = parse_args()
    payment_id = args.payment_id.strip()
    amount = args.amount.strip()
    expires_at = int(args.expires_at.strip())
    max_runtime = args.max_runtime_sec or int(os.getenv("PY_WATCHER_MAX_RUNTIME_SEC", "240"))
    interval = int(os.getenv("PY_WATCHER_INTERVAL_SEC", "8"))
    max_age_sec = int(os.getenv("GMAIL_MESSAGE_MAX_AGE_SEC", "30"))

    imap_user = (os.getenv("GMAIL_IMAP_USER") or "").strip()
    imap_pass = (os.getenv("GMAIL_IMAP_APP_PASSWORD") or "").strip()
    confirm_url = (os.getenv("AUTO_CONFIRM_URL") or "").strip()
    confirm_secret = (os.getenv("AUTO_PAYMENT_CONFIRM_SECRET") or "").strip()
    from_match = os.getenv("GMAIL_FROM_MATCH", "famapp.in,famapp").lower()
    from_tokens = [t.strip() for t in from_match.split(",") if t.strip()]

    if not imap_user or not imap_pass:
        print("Missing IMAP credentials", flush=True)
        return 1
    if not confirm_url:
        print("Missing AUTO_CONFIRM_URL", flush=True)
        return 1

    start_ts = int(time.time() * 1000)
    hard_deadline = min(expires_at, start_ts + max_runtime * 1000)

    log(f"[watcher] start payment_id={payment_id} amount={amount}")

    while int(time.time() * 1000) < hard_deadline:
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com")
            imap.login(imap_user, imap_pass)
            imap.select("INBOX")

            since_date = (dt.datetime.utcnow() - dt.timedelta(days=2)).strftime("%d-%b-%Y")
            status, data = imap.search(None, f'(SINCE {since_date})')
            if status != "OK":
                log("[watcher] IMAP search failed")
                imap.logout()
                time.sleep(interval)
                continue

            msg_ids = data[0].split()
            msg_ids = msg_ids[-30:]  # limit last 30 messages

            min_ts = max(int(time.time() * 1000) - max_age_sec * 1000, start_ts - 5000)

            for msg_id in reversed(msg_ids):
                status, parts = imap.fetch(msg_id, "(RFC822)")
                if status != "OK" or not parts:
                    continue

                msg = email.message_from_bytes(parts[0][1])
                subject = decode_mime_words(msg.get("Subject", ""))
                sender = decode_mime_words(msg.get("From", ""))

                if from_tokens:
                    sender_l = sender.lower()
                    if not any(t in sender_l for t in from_tokens):
                        continue

                msg_date = msg.get("Date")
                if msg_date:
                    try:
                        dt_obj = parsedate_to_datetime(msg_date)
                        if dt_obj.tzinfo:
                            msg_ts = int(dt_obj.timestamp() * 1000)
                        else:
                            msg_ts = int(dt_obj.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
                        if msg_ts < min_ts:
                            continue
                    except Exception:
                        pass

                body_text = extract_text_from_message(msg)
                combined = "\n".join([subject, sender, body_text])

                if not amount_matches(combined, amount):
                    continue
                if not message_looks_like_credit(combined):
                    continue

                txn_id = extract_txn_id(combined)
                snippet = (body_text or "")[:180]

                payload = {
                    "paymentId": payment_id,
                    "txnId": txn_id,
                    "snippet": snippet,
                    "subject": subject[:180],
                    "senderEmail": sender[:180],
                }
                if confirm_secret:
                    payload["confirmToken"] = confirm_secret
                else:
                    payload["secret"] = ""

                status_code, j, raw = post_confirm(confirm_url, payload)
                if status_code == 200 and j.get("success"):
                    log(f"[watcher] confirmed payment_id={payment_id} txn={txn_id}")
                    imap.logout()
                    return 0

                log(f"[watcher] confirm failed status={status_code} body={raw[:120]}")
                imap.logout()
                return 1

            imap.logout()
        except Exception as e:
            log(f"[watcher] error: {e}")

        time.sleep(interval)

    log("[watcher] timeout reached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
