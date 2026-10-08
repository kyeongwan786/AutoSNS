import unittest
from unittest.mock import patch

from core import cloud
from core.llm import create_openai_client


class OpenAIProxyClientTests(unittest.TestCase):
    def test_cloud_client_uses_session_token_and_server_proxy(self):
        with patch.object(cloud, "supabase_configured", return_value=True), \
                patch.object(cloud, "settings", return_value={
                    "supabase_url": "https://project.supabase.co/",
                }), patch("openai.OpenAI") as openai_client:
            result = create_openai_client("local-key-must-not-be-used", "user-session-token", timeout=5)

        self.assertIs(result, openai_client.return_value)
        openai_client.assert_called_once_with(
            api_key="user-session-token",
            base_url="https://project.supabase.co/functions/v1/openai-proxy/v1",
            timeout=5,
        )

    def test_cloud_client_requires_authenticated_session(self):
        with patch.object(cloud, "supabase_configured", return_value=True):
            with self.assertRaises(cloud.CloudError):
                create_openai_client("local-key", None)


if __name__ == "__main__":
    unittest.main()
