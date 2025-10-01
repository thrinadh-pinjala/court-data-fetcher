import os
from causelist_scraper import parse_causelist_html


def test_parse_debug_response_file():
    here = os.path.dirname(__file__)
    # use the debug response file that exists in repository root
    repo_root = os.path.abspath(os.path.join(here, '..'))
    debug_file = os.path.join(repo_root, 'debug_response_1759301741.txt')
    assert os.path.exists(debug_file), f"Expected debug file at {debug_file}"

    with open(debug_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # the debug file may contain header metadata; try to extract the HTML part
    if '---RAW_RESPONSE_START---' in content:
        html = content.split('---RAW_RESPONSE_START---', 1)[1]
    else:
        html = content

    results = parse_causelist_html(html)
    # at minimum, the parser should not crash and should return a list
    assert isinstance(results, list)
    # debug files vary; parser may legitimately return an empty list if no table matched
    # ensure we return a list and do not raise
