import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scrape import find_price_from_selectors


def test_uses_next_selector_when_first_matches_but_has_no_price():
    html = """
    <div class="container">
        <span>Không có giá</span>
    </div>
    <div class="price">1.234.567</div>
    """
    soup = BeautifulSoup(html, "lxml")

    price, selector = find_price_from_selectors(soup, [".container", ".price"])

    assert price == "1234567"
    assert selector == ".price"
