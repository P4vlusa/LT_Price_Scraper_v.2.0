import unittest

from scrape import extract_price_from_text


class ExtractPriceTests(unittest.TestCase):
    def test_extracts_vnd_price_with_thousand_separators(self):
        self.assertEqual(extract_price_from_text("Giá: 24.790.000 VNĐ"), "24790000")

    def test_extracts_price_from_attribute_like_content(self):
        self.assertEqual(extract_price_from_text('data-price="23990000"'), "23990000")

    def test_returns_none_when_price_is_not_available(self):
        self.assertIsNone(extract_price_from_text("Liên hệ"))


if __name__ == "__main__":
    unittest.main()
