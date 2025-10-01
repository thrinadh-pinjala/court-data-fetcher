# File: app.py

import os
import requests
import datetime
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from scraper import get_captcha, fetch_ecourts_data, fetch_search_form
from causelist_scraper import fetch_causelist_content, parse_causelist_html, parse_causelist_pdf_bytes
import logging
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    HAVE_APSCHED = True
except Exception:
    BackgroundScheduler = None
    HAVE_APSCHED = False
from sqlalchemy import Boolean

try:
    from weasyprint import HTML
except Exception:
    HTML = None

# --- App and Database Setup ---
basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__)
# A secret key is required for Flask sessions
app.config['SECRET_KEY'] = 'a-very-secret-key-that-you-should-change'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- API route for dynamic bench options ---
@app.route('/api/bench_options')
def api_bench_options():
    state = request.args.get('state', '').strip().lower()
    select_options = session.get('initial_form_data', {}).get('select_options', {})
    # Find the bench select
    bench_select = None
    for sel_name in select_options:
        if 'bench' in sel_name.lower() or 'location' in sel_name.lower():
            bench_select = sel_name
            break
    options = []
    if bench_select:
        for label, val in select_options[bench_select].items():
            options.append({'label': label, 'value': val})
    return jsonify({'options': options})


@app.route('/api/highcourt_options')
def api_highcourt_options():
    """Return High Court (cino) options as label/value pairs.
    Ensures initial_form_data is present in the session by fetching the
    search form if necessary.
    """
    # ensure we have initial_form_data saved
    initial = session.get('initial_form_data') or {}
    if not initial:
        # fetch and store initial form data
        scraper_session = requests.Session()
        scraper_session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        fetched, err = fetch_search_form(scraper_session)
        initial = fetched or {}
        session['initial_form_data'] = initial

    select_options = initial.get('select_options', {})

    # Heuristic: find the select that contains High Court labels or whose name
    # contains 'cino', 'court' or 'state'.
    cino_select = None
    for name, opts in select_options.items():
        lname = name.lower() if name else ''
        # check for obvious select names first
        if 'cino' in lname or 'court' in lname or 'state' in lname:
            cino_select = name
            break

    # if not found by name, look for a select that has labels mentioning 'high court'
    if not cino_select:
        for name, opts in select_options.items():
            for label in opts.keys():
                if 'high court' in label.lower() or 'high court of' in label.lower():
                    cino_select = name
                    break
            if cino_select:
                break

    options = []
    if cino_select:
        for label, val in select_options.get(cino_select, {}).items():
            options.append({'label': label, 'value': val})

    return jsonify({'options': options})

# --- Database Model ---
class CaseQuery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    case_type = db.Column(db.String(50), nullable=False)
    case_number = db.Column(db.String(50), nullable=False)
    case_year = db.Column(db.String(4), nullable=False)
    raw_response = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class Watch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    monitor_url = db.Column(db.String(1024), nullable=False)
    case_identifier = db.Column(db.String(256), nullable=False)  # what to search for (CNR or case no or party)
    email = db.Column(db.String(256), nullable=True)
    days_before = db.Column(db.Integer, default=1)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    last_notified = db.Column(db.DateTime, nullable=True)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    watch_id = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    matched_text = db.Column(db.Text, nullable=True)
    cause_url = db.Column(db.String(1024), nullable=True)

with app.app_context():
    db.create_all()

# Scheduler setup (do not start during testing)
scheduler = BackgroundScheduler() if HAVE_APSCHED else None


def check_watches():
    """Scheduled job that checks monitor URLs for matching cases."""
    with app.app_context():
        watches = Watch.query.filter_by(active=True).all()
        for w in watches:
            try:
                res = fetch_causelist_content(None, w.monitor_url)
                parsed = []
                if res['type'] == 'html':
                    parsed = parse_causelist_html(res['content'])
                elif res['type'] == 'pdf':
                    try:
                        parsed = parse_causelist_pdf_bytes(res['content'])
                    except Exception as e:
                        logging.exception('PDF parse failed for %s: %s', w.monitor_url, e)
                        parsed = []

                # naive matching: check if case_identifier substring exists in case_ref or parties
                for entry in parsed:
                    hay = (entry.get('case_ref', '') + ' ' + entry.get('parties', '')).lower()
                    if w.case_identifier.lower() in hay:
                        # create a notification record
                        note = Notification(watch_id=w.id, matched_text=str(entry)[:200], cause_url=w.monitor_url)
                        db.session.add(note)
                        # mark last_notified
                        w.last_notified = datetime.datetime.utcnow()
                        db.session.commit()
                        logging.info('Watch %s matched on URL %s', w.id, w.monitor_url)
                        break
            except Exception:
                logging.exception('Failed checking watch %s', w.id)


def start_scheduler():
    # don't start in test mode
    if app.config.get('TESTING'):
        return
    if not HAVE_APSCHED or scheduler is None:
        logging.info('APScheduler not available; scheduler disabled')
        return
    # scheduler exists
    try:
        running = getattr(scheduler, 'running', False)
    except Exception:
        running = False
    if not running:
        scheduler.add_job(check_watches, 'interval', minutes=1, id='check_watches')
        scheduler.start()

# --- Web Routes ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/fetch', methods=['POST'])
def fetch_captcha():
    # Determine which search criteria is selected
    search_criteria = None
    for criteria in ['cino', 'party_name', 'case_number', 'filing_number', 'advocate_name', 'fir_number', 'act', 'case_type_only']:
        if request.form.get(criteria) is not None:
            search_criteria = criteria
            break

    # Build case_details dictionary based on search criteria
    case_details = {
        'state': request.form.get('state'),
        'bench': request.form.get('bench'),
        'search_criteria': search_criteria,
        'cino': request.form.get('cino'),
        'party_name': request.form.get('party_name'),
        'case_type': request.form.get('case_type') or request.form.get('case_type_only'),
        'case_number': request.form.get('case_number'),
        'case_year': request.form.get('case_year'),
        'filing_number': request.form.get('filing_number'),
        'advocate_name': request.form.get('advocate_name'),
        'fir_number': request.form.get('fir_number'),
        'act': request.form.get('act'),
    }

    session['case_details'] = case_details
    
    scraper_session = requests.Session()
    # set a common User-Agent to reduce chance of anti-bot blocking
    scraper_session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    # fetch hidden form inputs/tokens so we can reuse them when submitting the search
    initial_form_data, form_error = fetch_search_form(scraper_session)
    captcha_path, error = get_captcha(scraper_session)
    
    if error:
        return f"Error fetching CAPTCHA: {error}"
        
    session['scraper_cookies'] = scraper_session.cookies.get_dict()
    # save any hidden inputs we fetched so they can be re-submitted with the search
    session['initial_form_data'] = initial_form_data or {}
    
    captcha_filename = os.path.basename(captcha_path)

    return render_template(
        'solve_captcha.html', 
        case_details=session['case_details'], 
        captcha_image_file=captcha_filename
    )

@app.route('/submit_captcha', methods=['POST'])
def submit_captcha():
    captcha_input = request.form.get('captcha')
    case_details = session.get('case_details', {})

    if not all([captcha_input, case_details]):
        return "Session expired or invalid data. Please start over."

    scraper_session = requests.Session()
    scraper_session.cookies.update(session.get('scraper_cookies', {}))
    scraper_session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })

    parsed_data, raw_html, error = fetch_ecourts_data(
        scraper_session,
        case_details,
        captcha_input,
        initial_form_data=session.get('initial_form_data')
    )
    
    # [cite_start]Store query and raw response in the database [cite: 1, 24]
    if case_details.get('search_criteria') == 'cino':
        new_query = CaseQuery(
            case_type='CNR',
            case_number=case_details.get('cino'),
            case_year='',
            raw_response=raw_html
        )
    else:
        new_query = CaseQuery(
            case_type=case_details.get('case_type') or case_details.get('case_type_only'),
            case_number=case_details.get('case_number'),
            case_year=case_details.get('case_year'),
            raw_response=raw_html
        )
    db.session.add(new_query)
    db.session.commit()
    
    if error or not parsed_data:
        return f"An error occurred: {error or 'No data found.'}"

    return render_template('results.html', data=parsed_data)


@app.route('/debug_form')
def debug_form():
    # returns whatever initial_form_data we saved when /fetch was called
    initial = session.get('initial_form_data') or {}
    return {
        'has_initial': bool(initial),
        'select_options_keys': list(initial.get('select_options', {}).keys()),
        'hidden_keys': list(initial.get('hidden', {}).keys()),
        'action': initial.get('action'),
        'method': initial.get('method')
    }


@app.route('/watches', methods=['GET', 'POST'])
def watches_view():
    if request.method == 'POST':
        monitor_url = request.form.get('monitor_url')
        case_identifier = request.form.get('case_identifier')
        email = request.form.get('email')
        days_before = int(request.form.get('days_before') or 1)
        w = Watch(monitor_url=monitor_url, case_identifier=case_identifier, email=email, days_before=days_before)
        db.session.add(w)
        db.session.commit()
        return redirect(url_for('watches_view'))

    watches = Watch.query.order_by(Watch.created_at.desc()).all()
    return render_template('watch.html', watches=watches)


@app.route('/watches/<int:watch_id>')
def watch_detail(watch_id):
    w = Watch.query.get_or_404(watch_id)
    notes = Notification.query.filter_by(watch_id=watch_id).order_by(Notification.timestamp.desc()).limit(20).all()
    return render_template('watch_detail.html', watch=w, notifications=notes)


@app.route('/causelist/to_pdf')
def causelist_to_pdf():
    """Fetch a cause-list URL and return a PDF. Query param: url=<monitor_url>

    If the URL is already a PDF, return it directly. If it's HTML, parse entries and render
    a simple HTML template then convert to PDF using WeasyPrint (if available).
    """
    url = request.args.get('url')
    if not url:
        return 'Missing url parameter', 400
    try:
        res = fetch_causelist_content(None, url)
    except Exception as e:
        return f'Failed to fetch URL: {e}', 500

    if res['type'] == 'pdf':
        # return raw PDF bytes
        return (res['content'], 200, {'Content-Type': 'application/pdf'})

    # HTML: parse and render template
    parsed = parse_causelist_html(res['content'])
    html_out = render_template('causelist.html', entries=parsed, source_url=url)

    if HTML is None:
        # WeasyPrint not available, return HTML instead
        return html_out

    try:
        pdf_bytes = HTML(string=html_out).write_pdf()
        return (pdf_bytes, 200, {'Content-Type': 'application/pdf'})
    except Exception as e:
        logging.exception('WeasyPrint failed: %s', e)
        return html_out

if __name__ == '__main__':
    # start background scheduler when running directly
    start_scheduler()
    app.run(debug=True)