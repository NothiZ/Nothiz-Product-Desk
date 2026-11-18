import types
import unittest
from unittest.mock import MagicMock,patch

import requests
import llm_client


class LlmNetworkErrorTests(unittest.TestCase):
    def test_deepseek_balance_formats_available_and_insufficient_amounts(self):
        session = MagicMock()
        with patch.object(llm_client, "_tls", types.SimpleNamespace(s=session)):
            for available, amount, label in ((True, "110.00", "可用"), (False, "0.00", "余额不足")):
                with self.subTest(available=available):
                    session.get.return_value.json.return_value = {
                        "is_available": available,
                        "balance_infos": [{"total_balance": amount, "currency": "CNY"}],
                    }
                    self.assertEqual(llm_client.get_deepseek_balance("test-key"), f"{label}：{amount} CNY")
                    session.get.assert_called_with(
                        "https://api.deepseek.com/user/balance",
                        headers={"Authorization": "Bearer test-key"}, timeout=15,
                    )

    def test_connection_failure_keeps_root_cause_and_does_not_sleep_after_last_attempt(self):
        session=MagicMock()
        session.post.side_effect=requests.ConnectionError("DNS lookup failed: getaddrinfo failed")
        with patch.object(llm_client,"_tls",types.SimpleNamespace(s=session)), \
             patch.object(llm_client.time,"sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError,"getaddrinfo failed"):
                llm_client._post("https://api.deepseek.com/v1/chat/completions",{},{"test":True})
        self.assertEqual(session.post.call_count,4)
        self.assertEqual(sleep.call_count,3)

    def test_diagnostic_redacts_key_and_proxy_password(self):
        session=MagicMock()
        key="private-test-key-not-real"
        error=f"Cannot connect https://user:private-password@proxy.local:8080; Bearer {key}"
        session.post.side_effect=requests.ConnectionError(error)
        with patch.object(llm_client,"_tls",types.SimpleNamespace(s=session)), \
             patch.object(llm_client.time,"sleep"), patch("builtins.print") as logs:
            with self.assertRaises(RuntimeError) as caught:
                llm_client._post("https://api.deepseek.com/v1/chat/completions",{"Authorization":f"Bearer {key}"},{})
        text=str(caught.exception)+" "+str(logs.call_args_list)
        self.assertNotIn(key,text)
        self.assertNotIn("private-password",text)
        self.assertIn("proxy.local",text)

    def test_http_auth_failure_does_not_retry(self):
        response=MagicMock(status_code=401)
        response.raise_for_status.side_effect=requests.HTTPError("401",response=response)
        session=MagicMock()
        session.post.return_value=response
        with patch.object(llm_client,"_tls",types.SimpleNamespace(s=session)), \
             patch.object(llm_client.time,"sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                llm_client._post("https://api.deepseek.com/v1/chat/completions",{}, {})
        session.post.assert_called_once()
        sleep.assert_not_called()

    def test_proxy_password_with_at_sign_is_fully_redacted(self):
        session=MagicMock()
        session.post.side_effect=requests.ConnectionError("Cannot connect https://user:pa@ss@proxy.local:8080")
        with patch.object(llm_client,"_tls",types.SimpleNamespace(s=session)), \
             patch.object(llm_client.time,"sleep"), patch("builtins.print") as logs:
            with self.assertRaises(RuntimeError) as caught:
                llm_client._post("https://api.deepseek.com/v1/chat/completions",{}, {})
        text=str(caught.exception)+str(logs.call_args_list)
        self.assertIn("https://[redacted]@proxy.local:8080",text)
        self.assertNotIn("ss@",text)


if __name__ == "__main__":
    unittest.main()
