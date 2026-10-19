"""Pure triage logic: parse emails, find order numbers, decide escalation, build payloads.

No network calls here, so everything is unit-tested.
"""

import email
import json
import re
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

CATEGORIES = ["Refund", "Shipping", "Dispute", "Product question", "Order change", "Other"]
URGENCIES = ["High", "Normal", "Low"]
SENTIMENTS = ["Angry", "Frustrated", "Neutral", "Happy"]

# Notion Customer Response Bank categories -> triage categories
TEMPLATE_CATEGORY_MAP = {
    "Refunds": "Refund", "Shipping": "Shipping", "Disputes": "Dispute",
    "Product": "Product question", "Payment": "Order change", "Custom Orders": "Order change",
}

HIGH_VALUE_ORDER = 200.0


# ---------- reading emails ----------

def _decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def strip_quoted(text):
    """Drop the quoted earlier thread and signatures so the AI sees only the new message."""
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(">"):
            continue
        if re.match(r"^On .+wrote:$", s) or s in ("-- ", "--") or s.startswith("-----Original Message"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def parse_email(raw_bytes):
    """Raw RFC822 bytes -> dict with sender, subject, plain-text body and threading ids."""
    msg = email.message_from_bytes(raw_bytes)
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                break
        if not body:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                    body = re.sub(r"<[^>]+>", " ", html)
                    break
    else:
        body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
    name, addr = parseaddr(_decode(msg.get("From")))
    return {
        "from_name": name,
        "from_email": addr.lower(),
        "subject": _decode(msg.get("Subject")) or "(no subject)",
        "body": strip_quoted(body)[:6000],
        "message_id": msg.get("Message-ID", ""),
        "references": msg.get("References", ""),
        "date": msg.get("Date", ""),
    }


ORDER_RE = re.compile(r"(?:order\s*(?:number|no\.?|#)?\s*:?\s*#?|#)(\d{4,6})\b", re.IGNORECASE)


def find_order_number(text):
    """'order #1012', 'Order 1012', '#1012' -> '#1012'. None if no plausible number."""
    m = ORDER_RE.search(text or "")
    return f"#{m.group(1)}" if m else None


# ---------- AI response handling ----------

def parse_ai_json(text):
    """Pull the first JSON object out of the model's reply and validate the fields."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        raise ValueError("AI reply contained no JSON")
    data = json.loads(m.group(0))
    out = {
        "category": data.get("category") if data.get("category") in CATEGORIES else "Other",
        "urgency": data.get("urgency") if data.get("urgency") in URGENCIES else "Normal",
        "sentiment": data.get("sentiment") if data.get("sentiment") in SENTIMENTS else "Neutral",
        "summary": str(data.get("summary", ""))[:500],
        "reply": str(data.get("reply", "")).strip(),
    }
    if not out["reply"]:
        raise ValueError("AI reply had no draft text")
    return out


KEYWORDS = [
    ("Dispute", ["chargeback", "dispute", "scam", "fraud", "lawyer", "report you", "bank"]),
    ("Refund", ["refund", "money back", "return", "reimburse"]),
    ("Shipping", ["where is my", "tracking", "shipped", "delivery", "arrived", "late", "lost", "package"]),
    ("Order change", ["cancel", "change my order", "address", "wrong size", "update my order"]),
    ("Product question", ["size", "material", "how do i", "does it", "in stock", "care"]),
]
ANGRY_WORDS = ["unacceptable", "ridiculous", "worst", "furious", "scam", "angry", "never again", "!!!"]


def keyword_triage(subject, body):
    """Fallback when the AI is unavailable: simple rules, clearly marked in the summary."""
    text = f"{subject}\n{body}".lower()
    category = next((c for c, words in KEYWORDS if any(w in text for w in words)), "Other")
    angry = any(w in text for w in ANGRY_WORDS)
    return {
        "category": category,
        "urgency": "High" if category == "Dispute" or angry else "Normal",
        "sentiment": "Angry" if angry else "Neutral",
        "summary": "[keyword fallback] " + (body.strip().splitlines() or [""])[0][:200],
        "reply": "",
    }


# ---------- decisions ----------

def escalation_reasons(triage, order):
    """Why a human should look now (empty list = no escalation)."""
    reasons = []
    if triage["category"] == "Dispute":
        reasons.append("dispute")
    if triage["sentiment"] == "Angry":
        reasons.append("angry customer")
    if triage["urgency"] == "High" and triage["category"] != "Dispute":
        reasons.append("high urgency")
    if order and order.get("total", 0) >= HIGH_VALUE_ORDER:
        reasons.append(f"high-value order (${order['total']:,.0f})")
    return reasons


def pick_templates(templates, category, limit=4):
    """Templates for the ticket's category first, so the prompt stays short and on-topic."""
    same = [t for t in templates if TEMPLATE_CATEGORY_MAP.get(t["category"], t["category"]) == category]
    return (same or templates)[:limit]


# ---------- outputs ----------

def build_draft(original, reply_text, from_addr):
    """A reply EmailMessage that threads under the customer's email in Gmail."""
    msg = EmailMessage()
    subj = original["subject"]
    msg["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    msg["From"] = from_addr
    msg["To"] = original["from_email"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if original.get("message_id"):
        msg["In-Reply-To"] = original["message_id"]
        msg["References"] = (original.get("references", "") + " " + original["message_id"]).strip()
    msg.set_content(reply_text)
    return msg


def _rt(text):
    return [{"type": "text", "text": {"content": (text or "")[:1900]}}]


def notion_properties(ticket):
    """Notion page properties for one ticket (Support Tickets database)."""
    props = {
        "Subject": {"title": _rt(ticket["subject"])},
        "Category": {"select": {"name": ticket["category"]}},
        "Urgency": {"select": {"name": ticket["urgency"]}},
        "Sentiment": {"select": {"name": ticket["sentiment"]}},
        "Status": {"status": {"name": "Escalated" if ticket["escalated"] else "Draft ready"}},
        "Summary": {"rich_text": _rt(ticket["summary"])},
        "Draft reply": {"rich_text": _rt(ticket["reply"])},
        "Escalated": {"checkbox": bool(ticket["escalated"])},
        "Received": {"date": {"start": ticket["received_iso"]}},
    }
    if ticket.get("from_email"):
        props["From"] = {"email": ticket["from_email"]}
    if ticket.get("order"):
        props["Order"] = {"rich_text": _rt(ticket["order"]["name"])}
        props["Order total"] = {"number": ticket["order"]["total"]}
    return props


def slack_text(ticket, reasons):
    order = f" · order {ticket['order']['name']} (${ticket['order']['total']:,.2f})" if ticket.get("order") else ""
    return (f":rotating_light: *Support escalation:* {', '.join(reasons)}\n"
            f"*{ticket['subject']}* from {ticket['from_email']}{order}\n"
            f"{ticket['category']} · {ticket['sentiment']} · {ticket['urgency']} urgency\n"
            f"> {ticket['summary']}\n"
            f"A draft reply is waiting in Gmail.")
