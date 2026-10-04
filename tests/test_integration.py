"""End-to-end integration test for the M40 monitoring foundation."""

import json
import tempfile
import unittest
from pathlib import Path

import agent
import scheduler
import storage


class _FakeLLMClient:
    def __init__(self, payload: dict):
        self.chat = self
        self.completions = self
        self.payload = payload

    def create(self, **kwargs):
        class Msg:
            content = json.dumps(self.payload)
        class Choice:
            finish_reason = "stop"
            message = Msg()
        class Resp:
            choices = [Choice()]
        return Resp()


class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = Path(self.tmp.name)
        storage.init_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_full_pipeline(self):
        instruction = "Monitor https://www.pbtech.co.nz/kindle and notify me when below NZD 300 every 30 minutes"
        parsed = {
            "schema_version": "1",
            "status": "ready",
            "product_name": "Kindle Paperwhite 16GB",
            "url": "https://www.pbtech.co.nz/kindle",
            "target_price": "300.00",
            "currency": "NZD",
            "comparison": "lt",
            "frequency_minutes": 30,
            "condition": None,
            "missing_fields": [],
            "clarification_question": None,
        }
        task = agent.parse_user_instruction(instruction, client=_FakeLLMClient(parsed), model="test")
        self.assertEqual(task["status"], "ready")

        stored_task = {
            "product_name": task["product_name"],
            "url": task["url"],
            "target_price": task["target_price"],
            "currency": task["currency"],
            "comparison": task["comparison"],
            "frequency_minutes": task["frequency_minutes"],
            "condition": task.get("condition"),
            "notify_email": "user@example.com",
            "status": "active",
        }
        task_id = storage.create_task(stored_task, db_path=self.db_path)

        def fetcher(url):
            return {
                "product_name": "Kindle Paperwhite 16GB",
                "url": url,
                "retailer": "pbtech.co.nz",
                "price": "299.00",
                "currency": "NZD",
                "in_stock": True,
                "fetched_at": "2026-09-23T04:00:00+00:00",
            }

        notifications = []

        def send_fn(email, task_obj, quote):
            notifications.append((email, quote["price"]))
            return {"success": True, "attempts": 1, "error": None}

        db_task = storage.get_task(task_id, db_path=self.db_path)
        check_result = scheduler.check_task(db_task, fetcher=fetcher, send_fn=send_fn, db_path=self.db_path)

        self.assertTrue(check_result["is_deal"])
        self.assertTrue(check_result["notified"])
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0][1], "299.00")


if __name__ == "__main__":
    unittest.main()
