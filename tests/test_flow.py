"""End-to-end flow with fake Gmail, Shopify, Claude, Notion and Slack (no network)."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import triage  # noqa: E402
from test_triage_core import raw_email  # noqa: E402


class FakeImap:
    def __init__(self):
        self.drafts, self.labelled = [], []

    def append(self, folder, flags, date, data):
        self.drafts.append((folder, data))

    def uid(self, cmd, uid, *args):
        if cmd == "store":
            self.labelled.append(uid)
        return "OK", [None]


CFG = {k: "" for k in ["ANTHROPIC_API_KEY", "SHOPIFY_STORE", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
                       "NOTION_TOKEN", "NOTION_TICKETS_DB", "NOTION_TEMPLATES_DB", "SLACK_WEBHOOK_URL", "SHOP_NAME"]}
CFG.update(GMAIL_ADDRESS="shop@gmail.com", GMAIL_APP_PASSWORD="x", SUPPORT_ADDRESS="shop+support@gmail.com",
           ANTHROPIC_MODEL="test", ANTHROPIC_API_KEY="key", NOTION_TOKEN="n", NOTION_TICKETS_DB="db",
           SLACK_WEBHOOK_URL="https://hooks.example/x")


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.orig_http, self.orig_order = triage.http_json, triage.shopify_order

        def fake_http(url, payload=None, headers=None, method=None):
            self.calls.append((url, payload))
            if "anthropic" in url:
                return {"content": [{"type": "text", "text": json.dumps({
                    "category": "Dispute", "urgency": "High", "sentiment": "Angry",
                    "summary": "Threatens chargeback over late order",
                    "reply": "Hi, I'm sorry for the wait. I've flagged order #1012 for our lead."})}]}
            return {}
        triage.http_json = fake_http
        triage.shopify_order = lambda cfg, name: {"name": name, "total": 250.0, "currency": "USD",
                                                   "date": "2026-10-10", "payment": "PAID",
                                                   "fulfillment": "UNFULFILLED", "items": ["1 x Mug"]}

    def tearDown(self):
        triage.http_json, triage.shopify_order = self.orig_http, self.orig_order

    def test_angry_dispute_goes_everywhere(self):
        imap = FakeImap()
        raw = raw_email("THIS IS UNACCEPTABLE", "Order #1012 still not here. Filing a chargeback!!!")
        ticket = triage.process(CFG, imap, b"7", raw, dry_run=False)
        self.assertTrue(ticket["escalated"])
        self.assertEqual(len(imap.drafts), 1)
        self.assertIn(b"Re: THIS IS UNACCEPTABLE", imap.drafts[0][1])
        self.assertEqual(imap.labelled, [b"7"])
        urls = [u for u, _ in self.calls]
        self.assertTrue(any("api.notion.com/v1/pages" in u for u in urls))
        self.assertTrue(any("hooks.example" in u for u in urls))

    def test_dry_run_changes_nothing(self):
        imap = FakeImap()
        triage.process(CFG, imap, b"8", raw_email("Where is order #1012?", "any update?"), dry_run=True)
        self.assertEqual(imap.drafts, [])
        self.assertEqual(imap.labelled, [])
        self.assertFalse(any("notion" in u or "hooks" in u for u, _ in self.calls))

    def test_ai_failure_falls_back_to_keywords(self):
        def broken(url, payload=None, headers=None, method=None):
            if "anthropic" in url:
                raise RuntimeError("529 overloaded")
            return {}
        triage.http_json = broken
        imap = FakeImap()
        t = triage.process(CFG, imap, b"9", raw_email("Refund please", "I want my money back for #1012"), False)
        self.assertEqual(t["category"], "Refund")
        self.assertTrue(t["summary"].startswith("[keyword fallback]"))
        self.assertEqual(len(imap.drafts), 1)


if __name__ == "__main__":
    unittest.main()
