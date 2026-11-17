import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

import desktop_runtime as runtime
import login
import scraper


class LoginStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.legacy = self.root / "project"
        self.legacy.mkdir()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "local"), "PRODUCT_DETAILS_DATA_DIR": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.resources = patch.object(runtime, "resource_dir", return_value=self.legacy)
        self.resources.start()
        self.addCleanup(self.resources.stop)

    def state_path(self, filename):
        helper = getattr(runtime, "login_state_path", None)
        self.assertIsNotNone(helper, "Shared login-state location is not implemented")
        return helper(filename)

    def test_legacy_state_moves_into_the_desktop_data_directory(self):
        legacy = self.legacy / "taobao_state.json"
        legacy.write_text("old-local-state", encoding="utf-8")
        destination = self.state_path(legacy.name)
        self.assertEqual(destination, self.root / "local/ProductDetailsDesktop/taobao_state.json")
        self.assertEqual(destination.read_text(encoding="utf-8"), "old-local-state")
        self.assertFalse(legacy.exists())

    def test_identical_duplicate_is_removed_without_overwriting_canonical_state(self):
        destination = runtime.user_data_dir() / "tmall_auth.json"
        destination.write_text("same-state", encoding="utf-8")
        legacy = self.legacy / destination.name
        legacy.write_text("same-state", encoding="utf-8")
        self.assertEqual(self.state_path(legacy.name), destination)
        self.assertFalse(legacy.exists())

    def test_conflicting_legacy_state_never_overwrites_existing_login(self):
        destination = runtime.user_data_dir() / "taobao_state.json"
        destination.write_text("current-state", encoding="utf-8")
        legacy = self.legacy / destination.name
        legacy.write_text("different-old-state", encoding="utf-8")
        self.assertEqual(self.state_path(legacy.name), destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), "current-state")
        self.assertEqual(legacy.read_text(encoding="utf-8"), "different-old-state")

    def test_explicit_data_directory_does_not_import_other_credentials(self):
        legacy = self.legacy / "alibaba_state.json"
        legacy.write_text("unrelated-state", encoding="utf-8")
        isolated = self.root / "isolated"
        with patch.dict(os.environ, {"PRODUCT_DETAILS_DATA_DIR": str(isolated)}):
            destination = self.state_path(legacy.name)
        self.assertEqual(destination, isolated / legacy.name)
        self.assertFalse(destination.exists())
        self.assertTrue(legacy.exists())

    def test_gui_migrates_legacy_states_before_starting_its_isolated_worker(self):
        source = self.root / "input.xlsx"
        source.touch()
        settings = {**runtime.default_settings(), "input_file": str(source),
                    "output_file": str(self.root / "output.xlsx")}
        for kind in ("login", "generate"):
            with self.subTest(kind=kind):
                for filename in scraper.STATE_FILES.values():
                    (self.legacy / filename).write_text("saved-state", encoding="utf-8")

                def spawn(*args, **kwargs):
                    data = Path(kwargs["env"]["PRODUCT_DETAILS_DATA_DIR"])
                    for filename in scraper.STATE_FILES.values():
                        self.assertEqual((data / filename).read_text(encoding="utf-8"), "saved-state")
                        self.assertFalse((self.legacy / filename).exists())
                    return MagicMock()

                with patch.object(runtime.subprocess, "Popen", side_effect=spawn):
                    runtime.launch_job(settings, kind=kind, api_key="test-key")


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

    def test_old_tmall_command_is_an_alias_for_the_combined_login(self):
        with patch.object(login, "StealthySession", return_value=self.session):
            self.assertTrue(login.login("tmall", confirm=lambda _: ""))
        self.assertEqual(self.session.fetch.call_args_list[0].args[0], "https://login.taobao.com")
        self.assertEqual(self.session.save_state.call_count, 2)

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
