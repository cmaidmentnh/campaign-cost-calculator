#!/usr/bin/env python3
"""
Campaign Cost Calculator
========================
Calculate voter outreach costs by district for NH political campaigns.
"""

import os
import json
import sqlite3
import requests
from functools import wraps
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32))

# Session security
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['WTF_CSRF_TIME_LIMIT'] = 3600

# CSRF protection
csrf = CSRFProtect(app)

# Rate limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

# Configuration
DB_PATH = Path(__file__).parent / 'calculator.db'
VOTER_API = os.environ.get('VOTER_API', 'http://138.197.36.143:5050')
VOTER_API_KEY = os.environ.get('VOTER_API_KEY', '')


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize database with default values."""
    conn = get_db()
    c = conn.cursor()

    # Prices table
    c.execute('''CREATE TABLE IF NOT EXISTS prices (
        id INTEGER PRIMARY KEY,
        tactic TEXT NOT NULL,
        component TEXT NOT NULL,
        price REAL NOT NULL,
        is_per_round INTEGER DEFAULT 1,
        description TEXT,
        UNIQUE(tactic, component)
    )''')

    # Settings table
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')

    # Quotes history
    c.execute('''CREATE TABLE IF NOT EXISTS quotes (
        id INTEGER PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        client_name TEXT,
        districts TEXT,
        filters TEXT,
        voter_count INTEGER,
        breakdown TEXT,
        grand_total REAL,
        notes TEXT
    )''')

    # Default prices
    defaults = [
        ('direct_mail', 'postage', 0.50, 1, 'USPS first class'),
        ('direct_mail', 'printing', 0.35, 1, 'Full color postcard'),
        ('sms', 'append', 0.05, 0, 'Phone number lookup'),
        ('sms', 'send', 0.025, 1, 'Per text message'),
        ('email', 'append', 0.03, 0, 'Email append'),
        ('email', 'send', 0.008, 1, 'Per email sent'),
    ]
    for tactic, component, price, is_per_round, desc in defaults:
        c.execute('''INSERT OR IGNORE INTO prices (tactic, component, price, is_per_round, description)
                     VALUES (?, ?, ?, ?, ?)''', (tactic, component, price, is_per_round, desc))

    # Default settings
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_password', ?)",
              (generate_password_hash('changeme'),))
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('discount_percent', '0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('company_name', '1772 Strategies')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('notes', 'Terms: 50% deposit required to begin. Balance due upon completion.')")

    conn.commit()
    conn.close()


def login_required(f):
    """Decorator to require admin login."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


def call_voter_api(endpoint, params=None):
    """Call the voter API with authentication."""
    try:
        headers = {'X-API-Key': VOTER_API_KEY} if VOTER_API_KEY else {}
        url = f"{VOTER_API}{endpoint}"
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Voter API error: {e}")
        return None


@app.after_request
def set_security_headers(response):
    """Add security headers to all responses."""
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response


# ============================================================================
# PUBLIC ROUTES
# ============================================================================

@app.route('/')
def calculator():
    """Main calculator page."""
    conn = get_db()
    prices = conn.execute('SELECT * FROM prices ORDER BY tactic, component').fetchall()
    settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}
    conn.close()

    # Organize prices by tactic
    price_data = {}
    for p in prices:
        if p['tactic'] not in price_data:
            price_data[p['tactic']] = {'components': [], 'total_per_round': 0}
        price_data[p['tactic']]['components'].append({
            'name': p['component'],
            'price': p['price'],
            'is_per_round': p['is_per_round'],
            'description': p['description']
        })
        if p['is_per_round']:
            price_data[p['tactic']]['total_per_round'] += p['price']

    # Don't block page load - districts/elections loaded via AJAX
    return render_template('calculator.html',
                          prices=price_data,
                          settings=settings,
                          districts={'house_districts': [], 'senate_districts': [], 'counties': [], 'geocoding_progress': {}},
                          elections=[])


@app.route('/api/districts')
@csrf.exempt
def api_districts():
    """Get districts list (proxied from voter API)."""
    result = call_voter_api('/api/districts')
    if result:
        return jsonify(result)
    return jsonify({'house_districts': [], 'senate_districts': [], 'counties': [], 'geocoding_progress': {}})


@app.route('/api/elections')
@csrf.exempt
def api_elections():
    """Get elections list (proxied from voter API)."""
    result = call_voter_api('/api/available-elections')
    if result:
        elections = result.get('elections', [])
        # Filter to state-level elections
        state_elections = [e for e in elections if e.get('type', '').upper() in ('STATE GENERAL', 'STATE PRIMARY')]
        return jsonify({'elections': state_elections[:20]})
    return jsonify({'elections': []})


@app.route('/api/voter-count', methods=['POST'])
@csrf.exempt
def api_voter_count():
    """Get voter count based on filters."""
    data = request.json or {}

    params = {}
    if data.get('districts'):
        params['districts'] = ','.join(data['districts'])
    if data.get('parties'):
        params['party'] = ','.join(data['parties'])
    if data.get('county'):
        params['county'] = data['county']
    if data.get('elections'):
        params['elections'] = ','.join(data['elections'])
    if data.get('min_elections'):
        params['min_elections'] = data['min_elections']

    result = call_voter_api('/api/district-counts', params)
    if result:
        return jsonify(result)
    return jsonify({'error': 'Failed to fetch voter count', 'total_voters': 0})


@app.route('/api/calculate', methods=['POST'])
@csrf.exempt
def api_calculate():
    """Calculate costs based on voter count and rounds."""
    data = request.json or {}
    voter_count = data.get('voter_count', 0)
    rounds = data.get('rounds', {})

    conn = get_db()
    prices = conn.execute('SELECT * FROM prices').fetchall()
    settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}
    conn.close()

    # Build price map
    price_map = {}
    for p in prices:
        price_map[(p['tactic'], p['component'])] = {
            'price': p['price'],
            'is_per_round': p['is_per_round'],
            'description': p['description']
        }

    discount_pct = float(settings.get('discount_percent', 0)) / 100

    tactics = {
        'direct_mail': {'name': 'Direct Mail', 'components': ['postage', 'printing']},
        'sms': {'name': 'SMS/Text', 'components': ['append', 'send']},
        'email': {'name': 'Email', 'components': ['append', 'send']},
    }

    breakdown = {}
    grand_total = 0

    for tactic_key, tactic_info in tactics.items():
        num_rounds = rounds.get(tactic_key, 0)
        if num_rounds == 0:
            continue

        tactic_total = 0
        components = []

        for comp in tactic_info['components']:
            price_info = price_map.get((tactic_key, comp), {'price': 0, 'is_per_round': 1, 'description': ''})
            unit_price = price_info['price']

            if price_info['is_per_round']:
                cost = voter_count * num_rounds * unit_price
                units = voter_count * num_rounds
                desc = f"{voter_count:,} x {num_rounds} rounds x ${unit_price:.3f}"
            else:
                cost = voter_count * unit_price
                units = voter_count
                desc = f"{voter_count:,} x ${unit_price:.3f} (one-time)"

            components.append({
                'name': comp.replace('_', ' ').title(),
                'unit_price': unit_price,
                'units': units,
                'cost': cost,
                'description': desc,
                'is_per_round': price_info['is_per_round']
            })
            tactic_total += cost

        breakdown[tactic_key] = {
            'name': tactic_info['name'],
            'rounds': num_rounds,
            'components': components,
            'subtotal': tactic_total
        }
        grand_total += tactic_total

    discount_amount = grand_total * discount_pct
    final_total = grand_total - discount_amount

    return jsonify({
        'voter_count': voter_count,
        'breakdown': breakdown,
        'subtotal': grand_total,
        'discount_percent': discount_pct * 100,
        'discount_amount': discount_amount,
        'grand_total': final_total
    })


@app.route('/api/save-quote', methods=['POST'])
@csrf.exempt
def api_save_quote():
    """Save a quote to history."""
    data = request.json or {}

    conn = get_db()
    conn.execute('''INSERT INTO quotes (client_name, districts, filters, voter_count, breakdown, grand_total, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                 (data.get('client_name', ''),
                  json.dumps(data.get('districts', [])),
                  json.dumps(data.get('filters', {})),
                  data.get('voter_count', 0),
                  json.dumps(data.get('breakdown', {})),
                  data.get('grand_total', 0),
                  data.get('notes', '')))
    quote_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'quote_id': quote_id})


# ============================================================================
# ADMIN ROUTES
# ============================================================================

@app.route('/admin/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def admin_login():
    """Admin login page."""
    if request.method == 'POST':
        password = request.form.get('password', '')
        conn = get_db()
        stored = conn.execute("SELECT value FROM settings WHERE key = 'admin_password'").fetchone()
        conn.close()

        if stored and check_password_hash(stored['value'], password):
            session['admin_logged_in'] = True
            return redirect(url_for('admin'))
        return render_template('admin_login.html', error='Invalid password')

    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    """Logout admin."""
    session.pop('admin_logged_in', None)
    return redirect(url_for('calculator'))


@app.route('/admin')
@login_required
def admin():
    """Admin dashboard."""
    conn = get_db()
    prices = conn.execute('SELECT * FROM prices ORDER BY tactic, component').fetchall()
    settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}
    quotes = conn.execute('SELECT * FROM quotes ORDER BY created_at DESC LIMIT 50').fetchall()
    conn.close()

    # Group prices by tactic
    grouped = {}
    for p in prices:
        if p['tactic'] not in grouped:
            grouped[p['tactic']] = []
        grouped[p['tactic']].append(dict(p))

    return render_template('admin.html',
                          prices=grouped,
                          settings=settings,
                          quotes=quotes)


@app.route('/admin/update-prices', methods=['POST'])
@login_required
def update_prices():
    """Update pricing."""
    data = request.json or {}

    conn = get_db()
    for item in data.get('prices', []):
        conn.execute('''UPDATE prices SET price = ?, description = ?
                        WHERE tactic = ? AND component = ?''',
                     (item['price'], item.get('description', ''),
                      item['tactic'], item['component']))
    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/admin/update-settings', methods=['POST'])
@login_required
def update_settings():
    """Update settings."""
    data = request.json or {}

    conn = get_db()
    for key, value in data.items():
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                     (key, str(value)))
    conn.commit()
    conn.close()

    return jsonify({'success': True})


@app.route('/admin/change-password', methods=['POST'])
@login_required
def change_password():
    """Change admin password."""
    data = request.json or {}
    new_password = data.get('password', '')

    if len(new_password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    conn = get_db()
    conn.execute("UPDATE settings SET value = ? WHERE key = 'admin_password'",
                 (generate_password_hash(new_password),))
    conn.commit()
    conn.close()

    return jsonify({'success': True})


# ============================================================================
# STARTUP
# ============================================================================

with app.app_context():
    init_db()

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("Campaign Cost Calculator")
    print("=" * 60)
    print(f"Calculator:  http://localhost:5009")
    print(f"Admin:       http://localhost:5009/admin")
    print(f"Default password: changeme")
    print("=" * 60 + "\n")
    app.run(host='0.0.0.0', port=5009, debug=True)
