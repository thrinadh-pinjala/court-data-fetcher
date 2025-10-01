import unittest
from app import app, db, CaseQuery
from flask import session

class CourtDataFetcherTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        with app.app_context():
            db.create_all()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_index_page_loads(self):
        response = self.app.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Indian Court Case Fetcher', response.data)

    def test_fetch_captcha_sets_session(self):
        # Simulate form data for fetch
        response = self.app.post('/fetch', data={
            'state': 'High Court of Andhra Pradesh',
            'bench': 'Principal Bench at Andhra Pradesh',
            'cino': 'ABC123456',
            'captcha': 'dummy'  # captcha not used here
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with self.app.session_transaction() as sess:
            self.assertIn('case_details', sess)
            self.assertEqual(sess['case_details']['cino'], 'ABC123456')

    def test_submit_captcha_invalid_session(self):
        # No session set, should return error
        response = self.app.post('/submit_captcha', data={'captcha': '1234'})
        self.assertIn(b'Session expired or invalid data', response.data)

    # Additional tests for scraper and database can be added here
    # Note: Full scraper tests require mocking external requests

if __name__ == '__main__':
    unittest.main()
