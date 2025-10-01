"""
Lightweight cause-list scraper utilities.

This module focuses on fetching and parsing HTML-based cause-lists (typically
from High Court services pages). It provides a simple HTML parser that looks
for tabular cause-lists and returns a normalized list of case dictionaries.

The parser is intentionally defensive: many court HTMLs vary in structure,
so we apply heuristics (header keyword matching) to find the table and
normalize rows.
"""
import requests
from bs4 import BeautifulSoup
import urllib.parse
import re
import io
from typing import List, Dict, Optional

try:
    # pdfminer.six for extracting text from PDFs
    from pdfminer.high_level import extract_text
except Exception:
    extract_text = None


def fetch_causelist_html(session: Optional[requests.Session], url: str, timeout: int = 15) -> str:
    """Fetch the cause-list HTML content for the given URL.

    If session is None, a temporary requests.Session will be used.
    """
    sess = session or requests.Session()
    resp = sess.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def fetch_causelist_content(session: Optional[requests.Session], url: str, timeout: int = 15) -> Dict:
    """Fetch content and detect whether it's HTML or PDF.

    Returns a dict: {'type': 'html'|'pdf', 'content': str|bytes, 'response': requests.Response}
    """
    sess = session or requests.Session()
    resp = sess.get(url, timeout=timeout, stream=True)
    resp.raise_for_status()

    ct = (resp.headers.get('Content-Type') or '').lower()
    if 'application/pdf' in ct or url.lower().endswith('.pdf'):
        # read bytes
        content = resp.content
        return {'type': 'pdf', 'content': content, 'response': resp}
    else:
        # treat as HTML
        text = resp.text
        return {'type': 'html', 'content': text, 'response': resp}


def parse_causelist_pdf_bytes(pdf_bytes: bytes) -> List[Dict]:
    """Extract text from PDF bytes and try to parse case lines heuristically.

    Returns a list of case dicts similar to parse_causelist_html output.
    If pdfminer is not installed, raises RuntimeError.
    """
    if extract_text is None:
        raise RuntimeError('pdfminer.six is required to parse PDFs. Please install pdfminer.six')

    # use pdfminer to extract text into a string
    try:
        text = extract_text(io.BytesIO(pdf_bytes))
    except Exception as e:
        # fallback: attempt simple decoding
        try:
            text = pdf_bytes.decode('utf-8', errors='replace')
        except Exception:
            raise

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    results = []

    # Simple heuristic: lines that contain keywords like 'Case', 'CNR', 'Case No', or look like 'No. 123/2025'
    case_like_re = re.compile(r'(cnr|case\s+no\.?|case\s+number|no\.|\b\d{1,5}\/\d{2,4}\b)', re.I)

    for line in lines:
        if case_like_re.search(line):
            # pull a possible date if present
            date_match = re.search(r'\b\d{1,2}[\-/]\d{1,2}[\-/]\d{2,4}\b', line)
            hearing_date = date_match.group(0) if date_match else ''
            results.append({
                'case_ref': line,
                'parties': '',
                'hearing_date': hearing_date,
                'bench': '',
                'raw_row_html': line,
            })

    return results


def parse_causelist_html(html: str) -> List[Dict]:
    """Parse HTML and return a list of case dicts.

    Each returned case dict contains at least the keys:
      - case_ref: raw reference text found in the row
      - parties: party listing text
      - hearing_date: if available (heuristic)
      - bench: if available (heuristic)
      - raw_row_html: the raw HTML for the row (useful for debugging)

    The parser searches for tables whose header contains court-y keywords
    and then iterates rows to extract columns heuristically.
    """
    soup = BeautifulSoup(html, 'html.parser')

    header_keywords = [
        'case type', 'case number', 'case no', 'case year', 'sr no',
        'petitioner', 'respondent', 'party', 'view', 'hearing', 'bench'
    ]

    def table_score(tbl):
        # score a table by how many header keywords are present in its header
        headers = []
        for th in tbl.find_all('th'):
            headers.append(th.get_text(separator=' ').strip().lower())
        if not headers:
            first_row = tbl.find('tr')
            if first_row:
                headers = [td.get_text(separator=' ').strip().lower() for td in first_row.find_all(['td', 'th'])]
        header_text = ' '.join(headers)
        score = sum(1 for k in header_keywords if k in header_text)
        return score

    # find the best candidate table
    candidate = None
    best_score = 0
    for table in soup.find_all('table'):
        s = table_score(table)
        if s > best_score:
            best_score = s
            candidate = table

    if not candidate or best_score == 0:
        # fallback: try to find any table with 'view' links or 'case' words
        for table in soup.find_all('table'):
            text = table.get_text(separator=' ').lower()
            if 'view' in text or 'case no' in text or 'case number' in text:
                candidate = table
                break

    if not candidate:
        return []

    results = []
    # iterate meaningful rows
    for tr in candidate.find_all('tr'):
        # skip header rows
        if tr.find('th'):
            continue
        cols = tr.find_all('td')
        if not cols:
            continue

        # Heuristics to extract main fields
        #  - case_ref: often in col index 1 (after sr no), otherwise first column
        case_ref = None
        parties = None
        hearing_date = None
        bench = None

        # join text of each cell for flexible matching
        texts = [c.get_text(separator=' ').strip() for c in cols]

        if len(cols) >= 2:
            # prefer second column as case reference when a serial no exists
            case_ref = texts[1]
        else:
            case_ref = texts[0]

        if len(cols) >= 3:
            parties = texts[2]

        # try to find hearing date or bench in remaining columns
        combined = ' '.join(texts).lower()
        # date regex looking for simple date formats (dd-mm-yyyy or dd/mm/yyyy)
        date_match = re.search(r'\b\d{1,2}[\-/]\d{1,2}[\-/]\d{2,4}\b', combined)
        if date_match:
            hearing_date = date_match.group(0)

        # bench heuristics: look for words like 'bench' or common bench names
        if 'bench' in combined:
            # pick the text containing bench
            m = re.search(r'([A-Za-z\s]{4,50}bench[\s\w,\-()]*)', combined)
            if m:
                bench = m.group(0).strip()

        results.append({
            'case_ref': case_ref or '',
            'parties': parties or '',
            'hearing_date': hearing_date or '',
            'bench': bench or '',
            'raw_row_html': str(tr)
        })

    return results


if __name__ == '__main__':
    # simple command-line sanity check (not used by tests)
    import sys
    sess = requests.Session()
    if len(sys.argv) >= 2:
        url = sys.argv[1]
        print('Fetching', url)
        html = fetch_causelist_html(sess, url)
        cases = parse_causelist_html(html)
        print('Found', len(cases), 'cases')
    else:
        print('Usage: python causelist_scraper.py <url>')


def fetch_highcourt_causelist(session: Optional[requests.Session], base_url: str = 'https://hcservices.ecourts.gov.in/hcservices/main.php', params: dict = None) -> List[Dict]:
    """Fetch a High Court cause-list page from `hcservices.ecourts.gov.in`.

    - If `params` is provided, it will be appended as query parameters.
    - Returns a list of parsed case dicts (may be empty).
    """
    url = base_url
    if params:
        url = url + ('?' + urllib.parse.urlencode(params))
    res = fetch_causelist_content(session, url)
    if res['type'] == 'html':
        return parse_causelist_html(res['content'])
    elif res['type'] == 'pdf':
        return parse_causelist_pdf_bytes(res['content'])
    return []


def fetch_district_causelist(session: Optional[requests.Session], base_url: str = 'https://services.ecourts.gov.in/ecourtindia_v6/', endpoint: str = None, params: dict = None) -> List[Dict]:
    """Fetch a District Court cause-list from `services.ecourts.gov.in/ecourtindia_v6/`.

    - `endpoint` may be a specific path under the base URL; if omitted, the base URL is used.
    - Returns a list of parsed case dicts.
    """
    if endpoint:
        url = urllib.parse.urljoin(base_url, endpoint)
    else:
        url = base_url
    if params:
        url = url + ('?' + urllib.parse.urlencode(params))
    res = fetch_causelist_content(session, url)
    if res['type'] == 'html':
        return parse_causelist_html(res['content'])
    elif res['type'] == 'pdf':
        return parse_causelist_pdf_bytes(res['content'])
    return []
