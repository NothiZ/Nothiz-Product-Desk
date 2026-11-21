import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

import login
import scraper


class SharedLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {"PRODUCT_DETAILS_DATA_DIR": str(self.data)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session

        def fetch(url, **kwargs):
            page = MagicMock()
            page.url = "https://www.tmall.com/" if "tmall" in url else "https://i.taobao.com/"
            kwargs["page_action"](page)
        self.session.fetch.side_effect = fetch

    def test_taobao_login_saves_both_states_after_tmall_sso(self):
        confirm = MagicMock(return_value="")
        with patch.object(login, "StealthySession", return_value=self.session):
            self.assertTrue(login.login("taobao", confirm=confirm))
        self.assertEqual([c.args[0] for c in self.session.fetch.call_args_list], ["https://login.taobao.com", "https://www.tmall.com/"])
        confirm.assert_called_once()
        self.assertEqual(self.session.save_state.call_args_list, [call(str(self.data / "taobao_state.json")), call(str(self.data / "tmall_auth.json"))])

    def test_cancelled_tmall_confirmation_preserves_existing_states(self):
        def fetch(url, **kwargs):
            page = MagicMock()
            page.url = "https://login.tmall.com/" if "tmall" in url else "https://i.taobao.com/"
            kwargs["page_action"](page)
        self.session.fetch.side_effect = fetch
        with patch.object(login, "StealthySession", return_value=self.session):
            self.assertFalse(login.login("taobao", confirm=lambda _: ""))
        self.session.save_state.assert_not_called()

    def test_scraper_uses_the_same_absolute_state_paths_as_login(self):
        for site, url in (("alibaba", "https://www.alibaba.com/product-detail/test_1.html"), ("taobao", "https://item.taobao.com/item.htm?id=1"), ("tmall", "https://detail.tmall.com/item.htm?id=1")):
            target = self.data / scraper.STATE_FILES[site]
            target.write_text("test-state", encoding="utf-8")
            with self.subTest(site=site):
                self.assertEqual(scraper._state_file(url), str(target))


if __name__ == "__main__":
    unittest.main()
