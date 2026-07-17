import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import memory
import naz_realtime_adapter_server as server


class NazRealtimeAdapterServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(memory, "DB_PATH", str(Path(self.tmp.name) / "server.sqlite3"))
        self.admin_patch = patch.object(main, "ADMIN_ID", 1)
        self.db_patch.start()
        self.admin_patch.start()
        memory.init_db()

    def tearDown(self) -> None:
        self.admin_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_final_summary_contract_persists_once(self) -> None:
        request = {
            "protocol": server.PROTOCOL,
            "request_id": "request_id_123456789012345",
            "operation": "final_summary",
            "user_id": 1,
            "session_id": "session_key_123456789012345",
            "summary": "Серверный итог",
        }
        first = server.handle_request(request)
        second = server.handle_request(request)
        self.assertTrue(first["ok"])
        self.assertEqual(first["receipt"], second["receipt"])
        self.assertTrue(first["saved"])
        self.assertFalse(second["saved"])
        with memory.db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM memory_items WHERE kind='realtime_voice_summary'"
            ).fetchone()["count"]
        self.assertEqual(count, 1)

    def test_contract_rejects_client_authority_fields(self) -> None:
        request = {
            "protocol": server.PROTOCOL,
            "request_id": "request_id_123456789012345",
            "operation": "final_summary",
            "user_id": 1,
            "session_id": "session_key_123456789012345",
            "summary": "Итог",
            "persona": "naz",
        }
        with self.assertRaises(ValueError):
            server.handle_request(request)


if __name__ == "__main__":
    unittest.main()
