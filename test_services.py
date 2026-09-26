import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import emailnator_service
import outlook_alias


class EmailnatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_uses_current_api(self):
        response = Mock(status_code=200)
        response.json.return_value = {"status": "success", "email": "a.b@gmail.com"}
        response.text = ""
        with patch.object(emailnator_service, "_post", AsyncMock(return_value=response)) as post:
            self.assertEqual(await emailnator_service.generate_email(["dotGmail"]), "a.b@gmail.com")
            post.assert_awaited_once_with("/api/generate-email", {"ids": [3]})

    async def test_message_list_maps_current_shape_and_drops_locked_rows(self):
        response = Mock(status_code=200)
        response.json.return_value = {"messages": [
            {"id": "one", "subject": "OTP", "time_ago": "now"},
            {"id": "old", "locked": True},
        ]}
        response.text = ""
        with patch.object(emailnator_service, "_post", AsyncMock(return_value=response)):
            messages = await emailnator_service.get_messages("a@gmail.com")
        self.assertEqual(messages[0]["messageID"], "one")
        self.assertEqual(messages[0]["time"], "now")
        self.assertEqual(len(messages), 1)


class OutlookAliasTests(unittest.TestCase):
    def test_generated_aliases_keep_original_mailbox(self):
        sets = outlook_alias.generate_sets("john.doe@outlook.com", 5)
        aliases = sets["plus"] + sets["dotplus"]
        self.assertTrue(aliases)
        self.assertEqual(sets["dot"], [])
        for alias in aliases:
            self.assertTrue(alias.startswith("john.doe+"))
            self.assertTrue(alias.endswith("@outlook.com"))


if __name__ == "__main__":
    unittest.main()