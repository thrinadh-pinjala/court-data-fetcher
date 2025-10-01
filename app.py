# File: app.py

import os
from flask import Flask, render_template, request, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
import requests
import datetime
from scraper import get_captcha, fetch_ecourts_data

# --- App and Database Setup ---
basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__)
# A secret key is required for Flask sessions
app.config['SECRET_KEY'] = 'a-very-secret-key-that-you-should-change' 
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- Database Model ---
class CaseQuery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    case_type = db.Column(db.String(50), nullable=False)
    case_number = db.Column(db.String(50), nullable=False)
    case_year = db.Column(db.String(4), nullable=False)
    raw_response = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)

with app.app_context():
    db.create_all()

# --- Web Routes ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/fetch', methods=['POST'])
def fetch_captcha():
    session['case_details'] = {
        'type': request.form.get('case_type'),
        'number': request.form.get('case_number'),
        'year': request.form.get('case_year')
    }
    
    scraper_session = requests.Session()
    captcha_path, error = get_captcha(scraper_session)
    
    if error:
        return f"Error fetching CAPTCHA: {error}"
        
    session['scraper_cookies'] = scraper_session.cookies.get_dict()
    
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
    
    parsed_data, raw_html, error = fetch_ecourts_data(scraper_session, case_details, captcha_input)
    
    # [cite_start]Store query and raw response in the database [cite: 1, 24]
    new_query = CaseQuery(
        case_type=case_details.get('type'),
        case_number=case_details.get('number'),
        case_year=case_details.get('year'),
        raw_response=raw_html
    )
    db.session.add(new_query)
    db.session.commit()
    
    if error or not parsed_data:
        return f"An error occurred: {error or 'No data found.'}"

    return render_template('results.html', data=parsed_data)

if __name__ == '__main__':
    app.run(debug=True)