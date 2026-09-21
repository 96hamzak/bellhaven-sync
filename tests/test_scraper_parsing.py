"""
Proof that "parse by label" actually survives a layout change.

The same three facts are expressed in three different markups below. A
position-based scraper ("take the second div") breaks on at least two of them.
The label-based one reads all three, and it keeps working when a new row is
inserted above the ones we care about.
"""
from bs4 import BeautifulSoup

from bellhaven.scraper import split_offerings, value_text_for

# Shape 1: label and value as siblings — the most common pattern.
SIBLINGS = """
<div class="detail">
  <div class="label">Address</div>
  <div class="value">1800 N Blanchard St<br>Findlay, OH 45840</div>
  <div class="label">Care Offerings</div>
  <div class="value"><span class="tag">Assisted Living</span><span class="tag">Memory Support</span></div>
  <div class="label">Administrator</div>
  <div class="value">Sam Pruitt</div>
  <div class="label">Phone</div>
  <div class="value">(231) 533-2969</div>
</div>
"""

# Shape 2: a definition list.
DEFINITION_LIST = """
<dl>
  <dt>Occupancy</dt><dd>92%</dd>
  <dt>Address</dt><dd>1800 N Blanchard St<br>Findlay, OH 45840</dd>
  <dt>Care Offerings</dt><dd><li>Assisted Living</li><li>Memory Support</li></dd>
  <dt>Administrator</dt><dd>Sam Pruitt</dd>
  <dt>Phone</dt><dd>(231) 533-2969</dd>
</dl>
"""

# Shape 3: label and value wrapped together, no sibling relationship,
# and an extra row inserted first to break anything position-based.
WRAPPED = """
<section>
  <div class="row"><h4>Room Count</h4><p>84</p></div>
  <div class="row"><h4>Address</h4><p>1800 N Blanchard St<br>Findlay, OH 45840</p></div>
  <div class="row"><h4>Care Offerings</h4><p><em>Assisted Living</em><em>Memory Support</em></p></div>
  <div class="row"><h4>Administrator</h4><p>Sam Pruitt</p></div>
  <div class="row"><h4>Phone</h4><p>(231) 533-2969</p></div>
</section>
"""


def _parse(html):
    soup = BeautifulSoup(html, "html.parser")
    address = value_text_for(soup, "Address")
    return {
        "street": address.split("\n")[0],
        "city_line": address.split("\n")[1] if "\n" in address else "",
        "offerings": split_offerings(soup),
        "administrator": value_text_for(soup, "Administrator"),
        "phone": value_text_for(soup, "Phone"),
    }


def test_all_three_layouts_yield_the_same_facts():
    for html in (SIBLINGS, DEFINITION_LIST, WRAPPED):
        parsed = _parse(html)
        assert parsed["street"] == "1800 N Blanchard St"
        assert parsed["city_line"] == "Findlay, OH 45840"
        assert parsed["administrator"] == "Sam Pruitt"
        assert parsed["phone"] == "(231) 533-2969"
        assert parsed["offerings"] == ["Assisted Living", "Memory Support"]


def test_offerings_are_read_as_separate_tags_not_split_from_flattened_text():
    """The flattened string reads 'Assisted LivingMemory Support' with no
    separator. Reading the tags one by one is what keeps them apart."""
    soup = BeautifulSoup(SIBLINGS, "html.parser")
    flat = soup.select_one(".value + .label + .value").get_text(strip=True)
    assert flat == "Assisted LivingMemory Support"      # the trap
    assert split_offerings(soup) == ["Assisted Living", "Memory Support"]


def test_unknown_label_returns_empty_rather_than_guessing():
    soup = BeautifulSoup(SIBLINGS, "html.parser")
    assert value_text_for(soup, "Medicare Rating") == ""


def test_a_new_row_at_the_top_does_not_shift_anything():
    """This is the whole argument for parsing by label."""
    assert _parse(WRAPPED)["street"] == "1800 N Blanchard St"
