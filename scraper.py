# File: scraper.py

import requests
from bs4 import BeautifulSoup
import time
import os
import re
import urllib.parse

SEARCH_FORM_URL = 'https://hcservices.ecourts.gov.in/hcservices/cases_qry/index_qry.php'

def get_captcha(session):
    """
    Downloads and saves the CAPTCHA image.
    Returns the path to the image so it can be displayed.
    """
    try:
        base_url = 'https://hcservices.ecourts.gov.in/hcservices/cases/case_no.php'
        captcha_url = 'https://hcservices.ecourts.gov.in/hcservices/securimage/securimage_show.php'
        
        session.get(base_url)
        
        captcha_response = session.get(captcha_url, stream=True)
        captcha_response.raise_for_status()

        # Ensure the 'static' directory exists
        if not os.path.exists('static'):
            os.makedirs('static')

        # Create a unique filename to avoid browser caching issues
        image_path = f'static/captcha_{int(time.time())}.png'
        with open(image_path, 'wb') as f:
            f.write(captcha_response.content)
            
        return image_path, None

    except requests.exceptions.RequestException as e:
        return None, str(e)


def fetch_search_form(session):
    """
    Fetches the search form page and returns a dict of hidden input names/values.
    This helps preserve server-side tokens that must be submitted with the search.
    """
    try:
        resp = session.get(SEARCH_FORM_URL)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        hidden = {}
        for inp in soup.find_all('input', type='hidden'):
            name = inp.get('name')
            if name:
                hidden[name] = inp.get('value', '')

        # build select options mapping: select_name -> { label: value }
        select_options = {}
        for sel in soup.find_all('select'):
            name = sel.get('name')
            if not name:
                continue
            opts = {}
            for opt in sel.find_all('option'):
                label = opt.get_text(separator=' ').strip()
                val = opt.get('value', '')
                opts[label] = val
            select_options[name] = opts

        # try to find the form action and method for the main search form
        form_tag = soup.find('form')
        action = None
        method = 'post'
        if form_tag:
            action = form_tag.get('action')
            method = form_tag.get('method', 'post').lower()

        initial = {
            'hidden': hidden,
            'select_options': select_options,
            'action': action,
            'method': method,
        }
        return initial, None
    except requests.exceptions.RequestException as e:
        return None, str(e)


def fetch_ecourts_data(session, case_details, captcha_input, initial_form_data=None):
    state = case_details.get('state', '').lower()
    """
    Submits the case details along with the solved CAPTCHA.
    """
    # This is the updated URL you found!
    search_url = 'https://hcservices.ecourts.gov.in/hcservices/cases_qry/index_qry.php?action_code=showRecords'

    try:
        # If the caller supplied pre-fetched hidden/form data, use that to
        # avoid mismatches caused by session tokens or server-side fields.
        form_data = {}
        post_url = search_url
        if initial_form_data:
            # initial_form_data has structure: { hidden, select_options, action, method }
            form_data.update(initial_form_data.get('hidden', {}))
            select_options = initial_form_data.get('select_options', {})
        else:
            select_options = {}
            # Approach: fetch the search form page first so we can pick the correct
            # select option values (court/bench, party/status) which vary per state.
            try:
                sf_resp = session.get(SEARCH_FORM_URL)
                sf_resp.raise_for_status()
                sf_soup = BeautifulSoup(sf_resp.text, 'html.parser')

                # collect hidden inputs to include in the submission
                for inp in sf_soup.find_all('input', type='hidden'):
                    name = inp.get('name')
                    if name:
                        form_data[name] = inp.get('value', '')

                # collect select options mapping
                for sel in sf_soup.find_all('select'):
                    name = sel.get('name')
                    if not name:
                        continue
                    opts = {}
                    for opt in sel.find_all('option'):
                        label = opt.get_text(separator=' ').strip()
                        val = opt.get('value', '')
                        opts[label] = val
                    select_options[name] = opts

            except requests.exceptions.RequestException:
                # if we can't fetch the form page, continue with sensible defaults
                pass

        # if the initial form provided an action, prefer it
        if initial_form_data and initial_form_data.get('action'):
            post_url = urllib.parse.urljoin(SEARCH_FORM_URL, initial_form_data.get('action'))
            # Approach: fetch the search form page first so we can pick the correct
            # select option values (court/bench, party/status) which vary per state.
            try:
                sf_resp = session.get(SEARCH_FORM_URL)
                sf_resp.raise_for_status()
                sf_soup = BeautifulSoup(sf_resp.text, 'html.parser')

                # collect hidden inputs to include in the submission
                for inp in sf_soup.find_all('input', type='hidden'):
                    name = inp.get('name')
                    if name:
                        form_data[name] = inp.get('value', '')

                # heuristics: find a select whose name contains 'cino' or 'court' and pick the selected state
                cino_name = None
                for sel in sf_soup.find_all('select'):
                    name = sel.get('name') or ''
                    text = ' '.join(o.get_text(separator=' ').strip().lower() for o in sel.find_all('option'))
                    if state in text and ('cino' in name.lower() or 'court' in name.lower() or 'state' in name.lower()):
                        cino_name = name
                        # pick the first option that mentions the selected state
                        chosen_val = None
                        for opt in sel.find_all('option'):
                            if state in opt.get_text(separator=' ').lower():
                                chosen_val = opt.get('value')
                                break
                        if chosen_val:
                            form_data[cino_name] = chosen_val
                        break

                # attempt to find a bench select and pick 'principal' if present
                for sel in sf_soup.find_all('select'):
                    name = sel.get('name') or ''
                    if 'bench' in name.lower() or 'location' in name.lower() or 'principal' in sel.get_text(separator=' ').lower():
                        for opt in sel.find_all('option'):
                            if 'principal' in opt.get_text(separator=' ').lower():
                                form_data[name] = opt.get('value')
                                break

                # attempt to find party type select (petitioner/respondent)
                for sel in sf_soup.find_all('select'):
                    name = sel.get('name') or ''
                    if 'party' in name.lower() or 'petitioner' in sel.get_text(separator=' ').lower() or 'respondent' in sel.get_text(separator=' ').lower():
                        # default to petitioner/responder agnostic search: pick first option
                        first_opt = sel.find('option')
                        if first_opt and name not in form_data:
                            form_data[name] = first_opt.get('value')
                        break

            except requests.exceptions.RequestException:
                # if we can't fetch the form page, continue with sensible defaults
                pass

        # sensible defaults and user-supplied values
        # default 'cino' fallback (if we didn't find via the form): keep HCTN01 as a fallback
        if 'cino' not in form_data:
            form_data.setdefault('cino', 'HCTN01')

        # Map the user-selected state/bench to the exact option values the form expects
        # Try common select names like cino, court, state for the HC, and bench/location for bench
        try:
            user_state = case_details.get('state') or ''
            user_bench = case_details.get('bench') or ''
            # find the select name that looks like cino or court
            cino_select = None
            for sel_name, opts in select_options.items():
                if 'cino' in sel_name.lower() or 'court' in sel_name.lower() or 'state' in sel_name.lower():
                    cino_select = sel_name
                    break
            if cino_select and user_state:
                # try exact match first
                found = None
                for label, val in select_options[cino_select].items():
                    if user_state.strip().lower() == label.strip().lower():
                        found = val
                        break
                # fallback: partial containment
                if not found:
                    for label, val in select_options[cino_select].items():
                        if user_state.strip().lower() in label.strip().lower():
                            found = val
                            break
                if found:
                    form_data[cino_select] = found

            # pick bench
            bench_select = None
            for sel_name, opts in select_options.items():
                if 'bench' in sel_name.lower() or 'location' in sel_name.lower():
                    bench_select = sel_name
                    break
            if bench_select and user_bench:
                found = None
                for label, val in select_options[bench_select].items():
                    if user_bench.strip().lower() == label.strip().lower():
                        found = val
                        break
                if not found:
                    for label, val in select_options[bench_select].items():
                        if user_bench.strip().lower() in label.strip().lower():
                            found = val
                            break
                if found:
                    form_data[bench_select] = found

        except Exception:
            # if mapping fails, continue using existing fallbacks
            pass

        # core search fields - use the keys stored in `app.py`
        form_data.update({
            'case_type': case_details.get('case_type', ''),
            'case_no': case_details.get('case_number', ''),
            'year': case_details.get('case_year', ''),
            'captcha_code': captcha_input,
        })

        # If user searched by party name or other criteria, include them too
        if case_details.get('cino'):
            form_data['cino'] = case_details.get('cino')
        if case_details.get('party_name'):
            # common ecourts param names vary; try a few likely names
            form_data.setdefault('party_name', case_details.get('party_name'))
            form_data.setdefault('party', case_details.get('party_name'))
        if case_details.get('filing_number'):
            form_data.setdefault('filing_no', case_details.get('filing_number'))
        if case_details.get('advocate_name'):
            form_data.setdefault('advocate', case_details.get('advocate_name'))
        if case_details.get('fir_number'):
            form_data.setdefault('fir_no', case_details.get('fir_number'))
        if case_details.get('act'):
            form_data.setdefault('act', case_details.get('act'))

        # if the site expects a status param (pending/disposed/both), try common names
        if not any(k for k in form_data.keys() if 'status' in k.lower() or 'case_status' in k.lower()):
            # many ecourt forms accept 'status' with 'P','D','B' or 'Pending','Disposed','Both'
            form_data.setdefault('status', 'B')
        
        # mimic the AJAX request the site makes (reduces validation rejections)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': 'https://hcservices.ecourts.gov.in',
            'Referer': 'https://hcservices.ecourts.gov.in/',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        }
        # post_url will be the form action if provided, otherwise fallback
        try:
            response = session.post(post_url, data=form_data, headers=headers)
        except Exception:
            # fallback to the known search_url if post fails
            response = session.post(search_url, data=form_data, headers=headers)
        response.raise_for_status()

        raw_html = response.text

        soup = BeautifulSoup(raw_html, 'html.parser')

        # Check for common error messages
        lowered = raw_html.lower()
        if "invalid captcha" in lowered or "record not found" in lowered or "no record found" in lowered:
            return None, raw_html, "Record Not Found or Invalid CAPTCHA."

        # Helper: find a table that contains any of the header keywords
        def find_results_table(soup):
            header_keywords = [
                'case type', 'case number', 'case year', 'sr no', 'petitioner', 'respondent', 'party', 'view'
            ]
            for table in soup.find_all('table'):
                # collect header text from th or first row
                headers = []
                for th in table.find_all('th'):
                    headers.append(th.get_text(separator=' ').strip().lower())
                if not headers:
                    # try first row cells
                    first_row = table.find('tr')
                    if first_row:
                        headers = [td.get_text(separator=' ').strip().lower() for td in first_row.find_all(['td', 'th'])]

                header_text = ' '.join(headers)
                if any(k in header_text for k in header_keywords):
                    return table
            return None

        results_table = find_results_table(soup)

        # Fallback: locate any 'View' links and use their containing table/rows
        if not results_table:
            view_links = soup.find_all('a', string=lambda s: s and 'view' in s.lower())
            if view_links:
                # try to find the table ancestor for the first view link
                for a in view_links:
                    ancestor_table = a.find_parent('table')
                    if ancestor_table:
                        results_table = ancestor_table
                        break

        if not results_table:
            # Debug: save detailed debug output (status, headers, url, body)
            debug_filename = f'debug_response_{int(time.time())}.txt'
            try:
                with open(debug_filename, 'w', encoding='utf-8') as f:
                    f.write(f"URL: {response.url}\n")
                    f.write(f"STATUS: {getattr(response, 'status_code', 'N/A')}\n")
                    f.write("HEADERS:\n")
                    try:
                        for k, v in response.headers.items():
                            f.write(f"{k}: {v}\n")
                    except Exception:
                        f.write(str(getattr(response, 'headers', '')) + "\n")
                    f.write("\n---RAW_RESPONSE_START---\n")
                    # write full body to help debugging JSON vs HTML responses
                    f.write(raw_html)
                    # also dump the POST payload we sent (if available) and cookies
                    try:
                        f.write("\n---FORM_DATA_SUBMITTED---\n")
                        f.write(str(form_data) + "\n")
                    except Exception:
                        f.write("(could not serialize form_data)\n")
                    try:
                        f.write("\n---COOKIES_IN_SESSION---\n")
                        f.write(str(session.cookies.get_dict()) + "\n")
                    except Exception:
                        f.write("(could not serialize cookies)\n")
                print(f"Debug: Saved detailed response to {debug_filename} for troubleshooting.")
            except Exception as e:
                print(f"Debug: Failed to write debug file: {e}")

            return None, raw_html, "Could not find the case details table on the page."

        # Parse the first result row we can find
        rows = results_table.find_all('tr')
        parsed_data = None

        for row in rows:
            # skip header-like rows
            if row.find('th'):
                continue
            cells = row.find_all('td')
            if not cells or len(cells) < 2:
                continue

            # Many ecourts tables use columns: Sr No | Case Type / Case Number / Case Year | Parties | View
            case_ref = cells[1].get_text(separator=' ').strip()
            parties = None
            if len(cells) >= 3:
                parties = cells[2].get_text(separator=' ').strip()

            # Try locate a detail page link in this row
            view_anchor = row.find('a', string=lambda s: s and 'view' in s.lower()) or row.find('a')
            detail_page_html = None
            if view_anchor and view_anchor.get('href'):
                detail_href = view_anchor.get('href')
                detail_url = urllib.parse.urljoin(response.url, detail_href)
                try:
                    detail_resp = session.get(detail_url)
                    detail_resp.raise_for_status()
                    detail_page_html = detail_resp.text
                except requests.exceptions.RequestException:
                    detail_page_html = None

            # Default placeholders
            filing_date = None
            next_hearing_date = None
            case_status = None
            judgment_link = None

            # If we fetched a detail page, try to parse standard key/value tables
            if detail_page_html:
                dsoup = BeautifulSoup(detail_page_html, 'html.parser')
                # Look for a details table (class may be 'table_val_ros' or similar)
                detail_table = dsoup.find('table', {'class': re.compile(r'table_val', re.I)})
                if not detail_table:
                    # try any table that contains label-like text
                    for t in dsoup.find_all('table'):
                        text = t.get_text(separator=' ').lower()
                        if 'petitioner' in text or 'respondent' in text or 'filed' in text or 'hearing' in text:
                            detail_table = t
                            break

                if detail_table:
                    # extract key/value rows
                    for tr in detail_table.find_all('tr'):
                        cols = tr.find_all('td')
                        if len(cols) >= 2:
                            label = cols[0].get_text(separator=' ').strip().lower()
                            value = cols[1].get_text(separator=' ').strip()
                            if 'filed' in label or 'filing' in label or 'date of filing' in label:
                                filing_date = value
                            if 'next' in label and 'hearing' in label:
                                next_hearing_date = value
                            if 'status' in label:
                                case_status = value
                    # look for judgment link
                    a_j = dsoup.find('a', href=True, string=lambda s: s and 'judgment' in s.lower())
                    if a_j:
                        judgment_link = urllib.parse.urljoin(detail_resp.url, a_j.get('href'))

            parsed_data = {
                'case_ref': case_ref,
                'parties': parties or 'Not Found',
                'filing_date': filing_date or 'Not Found',
                'next_hearing_date': next_hearing_date or 'Not Found',
                'case_status': case_status or 'Not Found',
                'judgment_link': judgment_link,
                'detail_page_html': detail_page_html
            }

            # we found a usable row; stop after first
            if parsed_data:
                break

        if not parsed_data:
            return None, raw_html, "No usable rows found in results table."

        return parsed_data, raw_html, None

    except requests.exceptions.RequestException as e:
        return None, None, str(e)