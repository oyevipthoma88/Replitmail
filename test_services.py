import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import emailnator_service
import gmail_alias
import microsoft_providers
import outlook_service


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


class NoPlusAddressTests(unittest.TestCase):
    """'+' wale address bahut sites par fail hote hain — kahin bhi nahi milne chahiye."""

    def test_gmail_aliases_never_contain_plus(self):
        sets = gmail_alias.generate_sets("john.doe.smith@gmail.com", 30)
        self.assertEqual(set(sets), {"dot"})
        self.assertTrue(sets["dot"])
        for alias in sets["dot"]:
            self.assertNotIn("+", alias)

    def test_emailnator_never_requests_plus_gmail(self):
        self.assertNotIn("plusGmail", emailnator_service.DEFAULT_OPTIONS)

    def test_22do_tag_is_stripped_to_real_base(self):
        self.assertEqual(outlook_service.base_address("abc123+x9k@outlook.com"), "abc123@outlook.com")

    def test_only_plus_free_providers_remain(self):
        self.assertEqual(set(microsoft_providers.PROVIDERS), {"22do", "kopeechka", "hotmailbox"})


class FreshMailboxTests(unittest.TestCase):
    def test_picks_empty_inbox_and_never_repeats(self):
        found = [("busy1@outlook.com", "d"), ("fresh1@hotmail.com", "d")]
        counts = {"busy1@outlook.com": 7, "fresh1@hotmail.com": 0}
        with patch.object(outlook_service, "_sync_pop_fresh", return_value=None), \
             patch.object(outlook_service, "_sync_collect", return_value=(found, "", 0)), \
             patch.object(outlook_service, "_inbox_count", side_effect=lambda a: counts[a]), \
             patch.object(outlook_service, "_mark_issued") as mark:
            r = outlook_service._sync_create_mailbox("microsoft")
        self.assertEqual(r["email"], "fresh1@hotmail.com")
        self.assertTrue(r["fresh"])
        mark.assert_called_once_with("fresh1@hotmail.com")


if __name__ == "__main__":
    unittest.main()