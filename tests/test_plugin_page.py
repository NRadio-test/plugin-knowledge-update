import unittest
from pathlib import Path


PAGE_ROOT = Path(__file__).resolve().parents[1] / "pages" / "knowledge-manager"


class PluginPageContractTests(unittest.TestCase):
    def test_delete_uses_in_page_confirmation_instead_of_blocked_browser_modal(self):
        html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
        script = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="confirm-dialog"', html)
        self.assertIn('id="confirm-submit"', html)
        self.assertIn("requestConfirmation(entry)", script)
        self.assertNotIn("window.confirm", script)

    def test_confirmed_delete_posts_the_exact_info_id_to_backend(self):
        script = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")

        self.assertIn("entries/${encodeURIComponent(entry.info_id)}", script)
        self.assertIn("confirm_info_id: entry.info_id", script)
        self.assertIn("await loadEntries(true)", script)


if __name__ == "__main__":
    unittest.main()
