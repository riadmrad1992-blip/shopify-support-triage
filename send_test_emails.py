"""Send realistic test customer emails to your support address, for demos.

Usage:
  python send_test_emails.py                 # sends 5 sample emails
  python send_test_emails.py --order "#1012" # use a real order number from your dev store
"""

import argparse
import smtplib
import time
from email.message import EmailMessage

from triage import load_env

SAMPLES = [
    ("Where is my order {order}?",
     "Hi, I ordered a ceramic mug last week (order {order}) and haven't received any shipping update. "
     "Can you tell me when it will arrive? Thanks, Sara"),
    ("Refund please",
     "Hello, the linen tote bag from order {order} is smaller than I expected. I'd like to return it "
     "and get my money back. What do I need to do? Mark"),
    ("THIS IS UNACCEPTABLE",
     "I paid for order {order} two weeks ago and still nothing. This is ridiculous. If I don't hear back "
     "today I'm filing a chargeback with my bank. Worst shopping experience ever!!!"),
    ("Question about the soy candle",
     "Hi! Is the soy candle scented or unscented, and how long does it burn? Thinking of buying three as gifts. Lena"),
    ("Change shipping address",
     "Hi, I just placed order {order} and realised I used my old address. Can you change it to "
     "14 Cedar Street, Springfield before it ships? Thank you! Omar"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", default="#1012", help="order number to mention, e.g. #1012")
    args = ap.parse_args()
    cfg = load_env()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(cfg["GMAIL_ADDRESS"], cfg["GMAIL_APP_PASSWORD"])
        for subject, body in SAMPLES:
            msg = EmailMessage()
            msg["Subject"] = subject.format(order=args.order)
            msg["From"] = cfg["GMAIL_ADDRESS"]
            msg["To"] = cfg["SUPPORT_ADDRESS"]
            msg.set_content(body.format(order=args.order))
            smtp.send_message(msg)
            print("sent:", msg["Subject"])
            time.sleep(1)


if __name__ == "__main__":
    main()
