import os
import sys

# Ensure protobuf check is disabled before importing neonize/agent
os.environ["TEMPORARILY_DISABLE_PROTOBUF_VERSION_CHECK"] = "true"

from datetime import datetime, timezone
import email
from email.message import EmailMessage
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import agent


class TestHTMLCleanup(unittest.TestCase):
    """Tests for HTML parsing, style/script stripping, and plain-text fallback."""

    def test_html_text_extractor_strips_scripts_and_styles(self):
        raw_html = """
        <html>
            <head>
                <style>body { color: red; } .hidden { display: none; }</style>
                <script>console.log("tracking pixel");</script>
            </head>
            <body>
                <h1>Special Weekend Sale!</h1>
                <p>Enjoy \u200b50% off on all items.\u200c</p>
                <div>Limited&nbsp;time&nbsp;offer.</div>
            </body>
        </html>
        """
        plain = agent.html_to_plain_text(raw_html)
        self.assertNotIn("body { color: red; }", plain)
        self.assertNotIn("console.log", plain)
        self.assertNotIn("\u200b", plain)
        self.assertNotIn("\u200c", plain)
        self.assertIn("Special Weekend Sale!", plain)
        self.assertIn("Enjoy 50% off on all items.", plain)
        self.assertIn("Limited time offer.", plain)

    def test_extract_plain_body_prefers_text_plain(self):
        msg = EmailMessage()
        msg["From"] = "test@example.com"
        msg["Subject"] = "Multipart Test"
        msg.set_content("This is genuine plain text.")
        msg.add_alternative("<p>This is HTML alternative.</p>", subtype="html")

        extracted = agent.extract_plain_body(msg)
        self.assertEqual(extracted, "This is genuine plain text.")

    def test_extract_plain_body_html_fallback(self):
        # Email has ONLY text/html part (no text/plain)
        msg = EmailMessage()
        msg["From"] = "newsletter@promo.com"
        msg["Subject"] = "HTML Only Promo"
        msg.set_payload("<style>p{margin:0}</style><p>Exclusive member deal: save $20 now!</p>")
        msg["Content-Type"] = "text/html; charset=utf-8"

        extracted = agent.extract_plain_body(msg)
        self.assertNotIn("<style>", extracted)
        self.assertNotIn("<p>", extracted)
        self.assertIn("Exclusive member deal: save $20 now!", extracted)

    def test_extract_plain_body_empty(self):
        msg = EmailMessage()
        msg["From"] = "empty@example.com"
        extracted = agent.extract_plain_body(msg)
        self.assertEqual(extracted, "[No plain-text body content]")


class TestIMAPSectionSeparation(unittest.TestCase):
    """Tests for multi-section Gmail IMAP fetching, BODY.PEEK[], and thread-context skipping."""

    @patch("agent.get_gmail_password", return_value="dummy-app-password")
    @patch("imaplib.IMAP4_SSL")
    def test_fetch_all_unread_emails_from_gmail(self, mock_imap_cls, mock_get_pw):
        mock_mail = MagicMock()
        mock_imap_cls.return_value.__enter__.return_value = mock_mail

        searches = []
        fetches = []

        def fake_search(charset, query):
            searches.append(query)
            if "category:promotions" in query and "-category:promotions" not in query:
                return "OK", [b"101"]
            elif "category:updates" in query and "-category:updates" not in query:
                return "OK", [b"102"]
            elif "category:social" in query and "-category:social" not in query:
                return "OK", [b"103"]
            else:
                # Primary inbox
                return "OK", [b"100"]

        mock_mail.search.side_effect = fake_search

        # Construct raw MIME message helper
        def make_raw_msg(subj, cat, date_str=None):
            m = EmailMessage()
            m["From"] = f"sender_{cat}@test.com"
            m["Subject"] = subj
            m["Message-ID"] = f"<{cat}-123@test.com>"
            m["Date"] = date_str or email.utils.formatdate(usegmt=True)
            m.set_content(f"Body content for {cat} email.")
            return m.as_bytes()

        def fake_fetch(eid, query):
            fetches.append((eid, query))
            eid_str = eid.decode("utf-8") if isinstance(eid, bytes) else str(eid)
            if eid_str == "100":
                raw = make_raw_msg("Important Meeting", "inbox")
                labels = b"(\\Inbox)"
            elif eid_str == "101":
                raw = make_raw_msg("50% Off Coupons", "promotions")
                labels = b"(\\Promotions)"
            elif eid_str == "102":
                raw = make_raw_msg("Security Alert Update", "updates")
                labels = b"(\\Updates)"
            else:
                raw = make_raw_msg("LinkedIn Connection", "social")
                labels = b"(\\Social)"
            return "OK", [(b"1 (X-GM-LABELS " + labels + b" BODY[]", raw), b")"]

        mock_mail.fetch.side_effect = fake_fetch

        with patch("agent.fetch_thread_context", return_value="[Prior Thread Context]") as mock_ctx:
            sections = agent.fetch_all_unread_emails_from_gmail(
                inbox_limit=15, category_limit=10, verbose=False
            )

            # Check that all 4 sections were fetched
            self.assertEqual(len(sections["inbox"]), 1)
            self.assertEqual(len(sections["promotions"]), 1)
            self.assertEqual(len(sections["updates"]), 1)
            self.assertEqual(len(sections["social"]), 1)

            # Check BODY.PEEK[] was used
            for _, q in fetches:
                self.assertIn("BODY.PEEK[]", q)

            # Check thread context was queried ONLY for inbox (eid 100), not for categories
            self.assertEqual(mock_ctx.call_count, 1)
            self.assertEqual(sections["inbox"][0]["thread_context"], "[Prior Thread Context]")
            self.assertEqual(sections["promotions"][0]["thread_context"], "")
            self.assertEqual(sections["updates"][0]["thread_context"], "")
            self.assertEqual(sections["social"][0]["thread_context"], "")


class TestCategorySummaryOllama(unittest.TestCase):
    """Tests for summary-only Ollama processing using qwen2.5:3b."""

    @patch("requests.post")
    def test_summarize_category_email_uses_correct_model_and_params(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {
                "content": json.dumps({
                    "category": "promotions",
                    "summary": "Swiggy offers 40% discount on orders above $10 until midnight."
                })
            }
        }
        mock_post.return_value = mock_response

        item = {
            "category": "promotions",
            "from": "offers@swiggy.in",
            "subject": "Hungry? 40% off your dinner!",
            "date": "Thu, 27 Nov 2026 20:00:00 +0000",
            "body": "Use code TASTY40 to get flat 40% off on all partner restaurants.",
        }

        result = agent.summarize_category_email(item)
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "promotions")
        self.assertEqual(
            result["summary"],
            "Swiggy offers 40% discount on orders above $10 until midnight."
        )
        self.assertNotIn("draft_body", result)
        self.assertNotIn("Best regards", str(result))

        # Verify Ollama call payload
        call_kwargs = mock_post.call_args[1]
        payload = call_kwargs["json"]
        self.assertEqual(payload["model"], "qwen2.5:3b")
        self.assertEqual(payload["keep_alive"], 0)
        self.assertEqual(payload["format"], agent.CATEGORY_SUMMARY_SCHEMA)
        self.assertEqual(payload["options"]["num_ctx"], 2048)
        self.assertEqual(payload["options"]["num_predict"], 180)


class TestInboxOnlyDrafting(unittest.TestCase):
    """Tests that reply drafting is strictly limited to Primary Inbox."""

    def test_process_email_skips_non_inbox_categories(self):
        for cat in ("promotions", "updates", "social"):
            item = {
                "category": cat,
                "from": f"news@{cat}.com",
                "subject": f"Check our {cat}",
                "body": "Some marketing or update content.",
            }
            draft = agent.process_email_and_generate_draft(item)
            self.assertIsNone(draft, f"Category '{cat}' should be skipped for drafting")

    @patch("requests.post")
    def test_process_email_drafts_for_inbox(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {
                "content": json.dumps({
                    "summary": "Client requesting updated invoice.",
                    "needs_reply": True,
                    "priority": "high",
                    "subject": "Re: Invoice Request",
                    "draft_body": "Hi John, attached is the invoice.\n\nBest regards,\nShubhsanket Sharma"
                })
            }
        }
        mock_post.return_value = mock_response

        item = {
            "category": "inbox",
            "from": "john.doe@clientcorp.com",
            "subject": "Invoice Request",
            "date": "Thu, 27 Nov 2026 10:00:00 +0000",
            "body": "Could you send over the latest invoice?",
            "thread_context": "",
        }

        draft = agent.process_email_and_generate_draft(item)
        self.assertIsNotNone(draft)
        self.assertTrue(draft["needs_reply"])
        self.assertIn("Shubhsanket Sharma", draft["draft_body"])


class TestDeduplicationAndWhatsAppRetry(unittest.TestCase):
    """Tests for section formatting, deduplication, and retry without re-invoking Ollama."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_summaries_file = Path(self.temp_dir.name) / "category_summaries.json"
        self.temp_drafts_file = Path(self.temp_dir.name) / "pending_drafts.json"

        # Patch agent file paths
        self.orig_summaries_file = agent.CATEGORY_SUMMARIES_FILE
        self.orig_drafts_file = agent.PENDING_DRAFTS_FILE
        agent.CATEGORY_SUMMARIES_FILE = self.temp_summaries_file
        agent.PENDING_DRAFTS_FILE = self.temp_drafts_file

        with open(self.temp_summaries_file, "w", encoding="utf-8") as f:
            json.dump({}, f)
        with open(self.temp_drafts_file, "w", encoding="utf-8") as f:
            json.dump({}, f)

    def tearDown(self):
        agent.CATEGORY_SUMMARIES_FILE = self.orig_summaries_file
        agent.PENDING_DRAFTS_FILE = self.orig_drafts_file
        self.temp_dir.cleanup()

    def test_format_category_summary_messages(self):
        summaries = [
            {
                "category": "promotions",
                "from": "Uber Eats",
                "subject": "50% off pizza",
                "summary": "Discount coupon for dinner orders.",
            },
            {
                "category": "updates",
                "from": "GitHub",
                "subject": "Security advisory",
                "summary": "Critical vulnerability patched in dependency.",
            },
            {
                "category": "social",
                "from": "LinkedIn",
                "subject": "You appeared in 12 searches",
                "summary": "Weekly search appearance report.",
            },
        ]
        messages = agent.format_category_summary_messages(summaries)
        self.assertEqual(len(messages), 3)

        promos_msg = next(m for m in messages if "*Promotions Email Summary*" in m)
        self.assertIn("• *From:* Uber Eats", promos_msg)
        self.assertIn("*Subject:* 50% off pizza", promos_msg)
        self.assertIn("*Summary:* Discount coupon for dinner orders.", promos_msg)

        updates_msg = next(m for m in messages if "*Updates Email Summary*" in m)
        self.assertIn("• *From:* GitHub", updates_msg)

        social_msg = next(m for m in messages if "*Social Email Summary*" in m)
        self.assertIn("• *From:* LinkedIn", social_msg)

    @patch("agent.get_or_set_whatsapp_recipient", return_value="919229803634")
    def test_dispatch_pending_retry_on_failure_without_re_invoking_ollama(self, mock_recip):
        mock_client = MagicMock()

        # Seed category_summaries.json with 1 undispatched item
        with open(self.temp_summaries_file, "w", encoding="utf-8") as f:
            json.dump({
                "item-1": {
                    "id": "item-1",
                    "category": "promotions",
                    "from": "Brand Deals",
                    "subject": "Exclusive Discount",
                    "summary": "Save $50 on annual subscription.",
                    "dispatched": False,
                }
            }, f)

        # 1. Simulate WhatsApp failure
        mock_client.send_message.side_effect = ConnectionError("WhatsApp network down")
        count = agent.dispatch_pending_category_summaries(mock_client)
        self.assertEqual(count, 0)

        # Verify item remains undispatched
        with open(self.temp_summaries_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertFalse(data["item-1"]["dispatched"])

        # 2. WhatsApp connection restored
        mock_client.send_message.side_effect = None
        count = agent.dispatch_pending_category_summaries(mock_client)
        self.assertEqual(count, 1)

        # Verify item is now marked dispatched
        with open(self.temp_summaries_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertTrue(data["item-1"]["dispatched"])
        self.assertIn("dispatched_at", data["item-1"])

        # 3. Third call should deduplicate and send nothing
        mock_client.send_message.reset_mock()
        count = agent.dispatch_pending_category_summaries(mock_client)
        self.assertEqual(count, 0)
        mock_client.send_message.assert_not_called()


class TestWhatsAppCommandHandling(unittest.TestCase):
    """Tests for WhatsApp command processing, outbound bot prefixes, and self-chat privacy."""

    def setUp(self):
        self.mock_client = MagicMock()

    def make_mock_msg(self, text: str, user: str = "919229803634"):
        msg = MagicMock()
        msg.Info.MessageSource.IsGroup = False
        msg.Info.MessageSource.IsFromMe = False
        msg.Info.MessageSource.Sender.User = user
        msg.Info.MessageSource.Chat.User = user
        return msg

    @patch("agent.get_or_set_whatsapp_recipient", return_value="919229803634")
    @patch("agent.get_authorized_identifiers", return_value={"919229803634"})
    def test_outbound_bot_prefixes_ignored(self, mock_auth, mock_recip):
        for prefix in agent.OUTBOUND_BOT_PREFIXES:
            msg = self.make_mock_msg(f"{prefix} test details")
            with patch("agent.extract_text", return_value=f"{prefix} test details"):
                agent.handle_incoming_whatsapp_message(self.mock_client, msg)
        self.mock_client.send_message.assert_not_called()

    @patch("agent.get_or_set_whatsapp_recipient", return_value="919229803634")
    @patch("agent.get_authorized_identifiers", return_value={"919229803634"})
    @patch("agent.scan_all_emails_and_dispatch", return_value={"new_drafts": 0, "new_summaries": 0})
    def test_scan_command_triggers_scan_all(self, mock_scan, mock_auth, mock_recip):
        for cmd in ("SCAN", "CHECK", "REFRESH", "STATUS"):
            msg = self.make_mock_msg(cmd)
            with patch("agent.extract_text", return_value=cmd):
                agent.handle_incoming_whatsapp_message(self.mock_client, msg)
        self.assertEqual(mock_scan.call_count, 4)

    @patch("agent.get_or_set_whatsapp_recipient", return_value="919229803634")
    @patch("agent.get_authorized_identifiers", return_value={"919229803634"})
    @patch("agent.scan_categories_and_dispatch", return_value=0)
    def test_summary_command_triggers_scan_categories(self, mock_summary, mock_auth, mock_recip):
        for cmd in ("SUMMARY", "SUMMARIES", "SUMMARIZE"):
            msg = self.make_mock_msg(cmd)
            with patch("agent.extract_text", return_value=cmd):
                agent.handle_incoming_whatsapp_message(self.mock_client, msg)
        self.assertEqual(mock_summary.call_count, 3)

    @patch("agent.get_or_set_whatsapp_recipient", return_value="919229803634")
    @patch("agent.get_authorized_identifiers", return_value={"919229803634"})
    def test_personal_notes_silently_ignored(self, mock_auth, mock_recip):
        notes = ["buy milk", "meeting notes for project X", "todo: call dentist"]
        for note in notes:
            msg = self.make_mock_msg(note)
            with patch("agent.extract_text", return_value=note):
                agent.handle_incoming_whatsapp_message(self.mock_client, msg)
        self.mock_client.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
