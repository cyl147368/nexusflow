import unittest

from webui import export_formats


class ExportFormatsTest(unittest.TestCase):
    def test_access_and_session_token_exports(self):
        row = {
            "email": "user@example.com",
            "access_token": "access-value",
            "session_token": "session-value",
        }

        self.assertEqual(
            export_formats.render_text([row], "at"),
            "access-value",
        )
        self.assertEqual(
            export_formats.render_text([row], "st"),
            "session-value",
        )
        self.assertEqual(
            export_formats.render_text([row], "at_st"),
            "access-value----session-value",
        )

    def test_token_formats_are_listed_for_the_ui(self):
        formats = {
            item["id"]: item
            for item in export_formats.list_formats()
        }
        self.assertIn("at", formats)
        self.assertIn("st", formats)
        self.assertIn("at_st", formats)
        self.assertEqual(formats["st"]["filename"], "ST.txt")
        self.assertEqual(formats["at_st"]["filename"], "AT_ST.txt")


if __name__ == "__main__":
    unittest.main()
