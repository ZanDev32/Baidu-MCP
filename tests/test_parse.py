"""Parser regression tests: the upstream fork returned None titles and redirect URLs."""

from baidu_mcp_server.server import html_to_markdown, parse_search_page

CARD = """
<div class="c-result result" new_srcid="1599" tpl="www_index">
  <div data-log='{{"mu":"{url}"}}'>
    <div data-module="title" role="title"><span class="cosc-title-a">Example &amp; Result</span></div>
  </div>
  <span class="sc-tag">Official</span>
  <div data-module="abstract"><div role="text"><span>Some snippet text.</span></div></div>
</div>
"""

WIDGET = """
<div class="c-result result" new_srcid="61344" tpl="new_baikan_index">
  <div data-log='{"mu":"https://ikan.baidu.com/pedia/"}'>AI answer widget</div>
</div>
"""


def _serp(*cards: str) -> str:
    return f"<html><body>{''.join(cards)}</body></html>"


def test_extracts_title_real_url_and_snippet():
    results = parse_search_page(
        _serp(CARD.format(url="https://pypi.org/project/mcp/"))
    )
    assert len(results) == 1
    result = results[0]
    assert result.title == "Example & Result"
    assert result.url == "https://pypi.org/project/mcp/"
    assert "Some snippet text." in result.snippet
    assert "Official" in result.snippet
    assert result.position == 1


def test_skips_widget_cards_and_baidu_internal_links():
    html = _serp(
        WIDGET,
        CARD.format(url="https://m.baidu.com/from=0/s?word=x"),
        CARD.format(url="https://example.com/a"),
    )
    results = parse_search_page(html)
    assert [r.url for r in results] == ["https://example.com/a"]


def test_deduplicates_repeated_urls():
    html = _serp(
        CARD.format(url="https://example.com/a"),
        CARD.format(url="https://example.com/a"),
    )
    assert len(parse_search_page(html)) == 1


def test_html_to_markdown_strips_scripts():
    markdown = html_to_markdown(
        "<html><body><script>alert(1)</script><p>Hello <b>world</b></p></body></html>"
    )
    assert "Hello" in markdown
    assert "alert(1)" not in markdown
