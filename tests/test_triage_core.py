"""Unit tests for the triage logic. Run with:  python -m unittest discover -s tests -v"""

import sys
import unittest
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import triage_core as core  # noqa: E402


def raw_email(subject, body, sender="Sara Lee <sara@example.com>"):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = subject, sender, "shop+support@gmail.com"
    m["Message-ID"] = "<abc123@example.com>"
    m["Date"] = "Mon, 19 Oct 2026 10:00:00 +0300"
    m.set_content(body)
    return m.as_bytes()


class ParseTests(unittest.TestCase):
    def test_parse_basic_fields(self):
        p = core.parse_email(raw_email("Where is my order?", "Hi, any update?"))
        self.assertEqual(p["from_email"], "sara@example.com")
        self.assertEqual(p["from_name"], "Sara Lee")
        self.assertEqual(p["subject"], "Where is my order?")
        self.assertIn("any update", p["body"])

    def test_quoted_thread_is_removed(self):
        body = "Still waiting!\n\nOn Mon, Oct 19, 2026 Shop wrote:\n> We shipped it\n> yesterday"
        self.assertEqual(core.strip_quoted(body), "Still waiting!")

    def test_html_only_email(self):
        m = EmailMessage()
        m["Subject"], m["From"] = "Hi", "a@b.com"
        m.add_alternative("<p>Order <b>#1012</b> please</p>", subtype="html")
        self.assertIn("#1012", core.parse_email(m.as_bytes())["body"])


class OrderNumberTests(unittest.TestCase):
    def test_formats(self):
        for text in ["order #1012", "Order 1012 is late", "my order number: 1012", "about #1012"]:
            self.assertEqual(core.find_order_number(text), "#1012", text)

    def test_no_false_positive_on_prices_or_years(self):
        self.assertIsNone(core.find_order_number("I paid 45 dollars in 2026"))


class AIParseTests(unittest.TestCase):
    def test_valid_json_inside_text(self):
        out = core.parse_ai_json('Sure:\n{"category":"Refund","urgency":"Normal","sentiment":"Neutral",'
                                 '"summary":"wants refund","reply":"Hi Mark"}')
        self.assertEqual(out["category"], "Refund")

    def test_unknown_values_fall_back_safely(self):
        out = core.parse_ai_json('{"category":"Billing","urgency":"ASAP","sentiment":"meh","summary":"x","reply":"Hi"}')
        self.assertEqual((out["category"], out["urgency"], out["sentiment"]), ("Other", "Normal", "Neutral"))

    def test_missing_reply_is_an_error(self):
        with self.assertRaises(ValueError):
            core.parse_ai_json('{"category":"Refund","reply":""}')


class KeywordTests(unittest.TestCase):
    def test_chargeback_is_dispute_and_high(self):
        t = core.keyword_triage("Help", "I'm filing a chargeback with my bank, this is unacceptable")
        self.assertEqual((t["category"], t["urgency"], t["sentiment"]), ("Dispute", "High", "Angry"))

    def test_tracking_is_shipping(self):
        self.assertEqual(core.keyword_triage("Tracking?", "where is my package")["category"], "Shipping")


class EscalationTests(unittest.TestCase):
    base = {"category": "Shipping", "urgency": "Normal", "sentiment": "Neutral"}

    def test_calm_shipping_question_not_escalated(self):
        self.assertEqual(core.escalation_reasons(self.base, {"total": 40}), [])

    def test_dispute_angry_and_high_value(self):
        t = dict(self.base, category="Dispute", sentiment="Angry")
        reasons = core.escalation_reasons(t, {"total": 450})
        self.assertIn("dispute", reasons)
        self.assertIn("angry customer", reasons)
        self.assertTrue(any("high-value" in r for r in reasons))


class OutputTests(unittest.TestCase):
    def test_draft_threads_under_original(self):
        orig = core.parse_email(raw_email("Refund please", "hi"))
        d = core.build_draft(orig, "Hi Sara, ...", "shop@gmail.com")
        self.assertEqual(d["Subject"], "Re: Refund please")
        self.assertEqual(d["To"], "sara@example.com")
        self.assertEqual(d["In-Reply-To"], "<abc123@example.com>")

    def test_notion_properties(self):
        ticket = {"subject": "Refund please", "category": "Refund", "urgency": "Normal", "sentiment": "Neutral",
                  "summary": "s", "reply": "r", "escalated": True, "received_iso": "2026-10-19T10:00:00+03:00",
                  "from_email": "sara@example.com", "order": {"name": "#1012", "total": 89.0}}
        p = core.notion_properties(ticket)
        self.assertEqual(p["Status"]["status"]["name"], "Escalated")
        self.assertEqual(p["Order total"]["number"], 89.0)
        self.assertEqual(p["Subject"]["title"][0]["text"]["content"], "Refund please")

    def test_templates_for_category_come_first(self):
        tpl = [{"category": "Shipping"}, {"category": "Refunds"}, {"category": "Disputes"}]
        self.assertEqual(core.pick_templates(tpl, "Refund")[0]["category"], "Refunds")


if __name__ == "__main__":
    unittest.main()
