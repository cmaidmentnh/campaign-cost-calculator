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

# Load .env from the same directory as this script
load_dotenv(Path(__file__).parent / '.env')

app = Flask(__name__)

# Session cookie hardening.
#   SECURE   - never send the session cookie over an unencrypted connection.
#              Flask's default is False, which means one plain http:// request,
#              made before the redirect to https, leaks the cookie to anyone on
#              the network path. Whoever copies it is logged in as that user.
#   HTTPONLY - JavaScript cannot read it, so injected script cannot steal it.
#   SAMESITE - not sent when another site triggers a request here, which stops a
#              malicious page acting as a logged-in user.
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32))

# Session security
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['WTF_CSRF_TIME_LIMIT'] = 3600

# CSRF protection
csrf = CSRFProtect(app)

# Rate limiting
def real_client_ip():
    """The visitor's address, not the proxy's.

    flask-limiter's get_remote_address returns request.remote_addr, which behind
    nginx is always 127.0.0.1. Every per-IP limit then shares one counter, so a
    single client can exhaust the login limit for everyone. nginx sets X-Real-IP
    from the connection it actually sees, so it cannot be forged; the left-most
    X-Forwarded-For entry can be, and is deliberately not used.
    """
    from flask import request as _rq
    try:
        return (_rq.headers.get('CF-Connecting-IP')
                or _rq.headers.get('X-Real-IP')
                or get_remote_address())
    except Exception:
        return get_remote_address()


limiter = Limiter(
    app=app,
    key_func=real_client_ip,
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
        pricing_model TEXT DEFAULT 'per_unit',
        UNIQUE(tactic, component)
    )''')

    # Volume tiers for dynamic pricing
    c.execute('''CREATE TABLE IF NOT EXISTS volume_tiers (
        id INTEGER PRIMARY KEY,
        tactic TEXT NOT NULL,
        min_qty INTEGER NOT NULL,
        max_qty INTEGER,
        discount_percent REAL NOT NULL DEFAULT 0,
        UNIQUE(tactic, min_qty)
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

    # Default prices - voter contact tactics
    defaults = [
        ('direct_mail', 'postage', 0.50, 1, 'USPS first class', 'per_unit'),
        ('direct_mail', 'printing', 0.35, 1, 'Full color postcard', 'per_unit'),
        ('sms', 'append', 0.05, 0, 'Phone number lookup', 'per_unit'),
        ('sms', 'send', 0.025, 1, 'Per text message', 'per_unit'),
        ('email', 'append', 0.03, 0, 'Email append', 'per_unit'),
        ('email', 'send', 0.008, 1, 'Per email sent', 'per_unit'),
        # Digital advertising - CPM based
        ('facebook', 'cpm', 4.00, 1, 'Cost per 1000 impressions', 'cpm'),
        ('facebook', 'management', 500.00, 0, 'Monthly management fee', 'flat'),
        ('ctv', 'cpm', 25.00, 1, 'Connected TV cost per 1000', 'cpm'),
        ('ctv', 'management', 750.00, 0, 'Monthly management fee', 'flat'),
        ('display', 'cpm', 3.00, 1, 'Display ads cost per 1000', 'cpm'),
        ('display', 'management', 400.00, 0, 'Monthly management fee', 'flat'),
    ]
    for tactic, component, price, is_per_round, desc, model in defaults:
        c.execute('''INSERT OR IGNORE INTO prices (tactic, component, price, is_per_round, description, pricing_model)
                     VALUES (?, ?, ?, ?, ?, ?)''', (tactic, component, price, is_per_round, desc, model))

    # Default volume tiers (discount % at quantity thresholds)
    volume_defaults = [
        ('direct_mail', 0, 9999, 0),
        ('direct_mail', 10000, 49999, 5),
        ('direct_mail', 50000, 99999, 10),
        ('direct_mail', 100000, None, 15),
        ('sms', 0, 9999, 0),
        ('sms', 10000, 49999, 5),
        ('sms', 50000, None, 10),
        ('email', 0, 24999, 0),
        ('email', 25000, 99999, 10),
        ('email', 100000, None, 20),
    ]
    for tactic, min_q, max_q, disc in volume_defaults:
        c.execute('''INSERT OR IGNORE INTO volume_tiers (tactic, min_qty, max_qty, discount_percent)
                     VALUES (?, ?, ?, ?)''', (tactic, min_q, max_q, disc))

    # Default settings
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_password', ?)",
              (generate_password_hash('changeme'),))
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('discount_percent', '0')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('company_name', '1772 Strategies')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('notes', 'Terms: 50% deposit required to begin. Balance due upon completion.')")
    # Digital ad settings
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('digital_match_rate', '60')")  # 60% match rate for digital targeting

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
        return jsonify({'elections': elections})
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

    # Handle vote filters (new format with vote_method)
    if data.get('vote_filters'):
        import json
        params['vote_filters'] = json.dumps(data['vote_filters'])
    if data.get('min_elections'):
        params['min_elections'] = data['min_elections']

    result = call_voter_api('/api/district-counts', params)
    if result:
        return jsonify(result)
    return jsonify({'error': 'Failed to fetch voter count', 'total_voters': 0})


@app.route('/api/calculate', methods=['POST'])
@csrf.exempt
def api_calculate():
    """Calculate costs based on voter count, rounds, and digital settings."""
    data = request.json or {}
    voter_count = data.get('voter_count', 0)
    rounds = data.get('rounds', {})
    digital = data.get('digital', {})  # New impressions-based digital ads

    conn = get_db()
    prices = conn.execute('SELECT * FROM prices').fetchall()
    volume_tiers = conn.execute('SELECT * FROM volume_tiers ORDER BY tactic, min_qty').fetchall()
    settings = {row['key']: row['value'] for row in conn.execute('SELECT * FROM settings').fetchall()}
    conn.close()

    # Build price map
    price_map = {}
    for p in prices:
        price_map[(p['tactic'], p['component'])] = {
            'price': p['price'],
            'is_per_round': p['is_per_round'],
            'description': p['description'],
            'pricing_model': p['pricing_model'] if 'pricing_model' in p.keys() else 'per_unit'
        }

    # Build volume tier map
    tier_map = {}
    for t in volume_tiers:
        if t['tactic'] not in tier_map:
            tier_map[t['tactic']] = []
        tier_map[t['tactic']].append({
            'min': t['min_qty'],
            'max': t['max_qty'],
            'discount': t['discount_percent']
        })

    def get_volume_discount(tactic, qty):
        """Get volume discount percentage for a tactic at given quantity."""
        tiers = tier_map.get(tactic, [])
        for tier in tiers:
            if qty >= tier['min'] and (tier['max'] is None or qty <= tier['max']):
                return tier['discount'] / 100
        return 0

    discount_pct = float(settings.get('discount_percent', 0)) / 100

    # Match rates for tactics (percentage of voters we can reach)
    mail_match_rate = float(settings.get('mail_match_rate', 70)) / 100  # Default 70% have valid mailing addresses
    sms_match_rate = float(settings.get('sms_match_rate', 40)) / 100
    email_match_rate = float(settings.get('email_match_rate', 50)) / 100  # Default 50% for email

    # Voter contact tactics (per-unit pricing)
    voter_tactics = {
        'direct_mail': {'name': 'Direct Mail', 'components': ['postage', 'printing'], 'match_rate': mail_match_rate},
        'sms': {'name': 'SMS/Text', 'components': ['append', 'send'], 'match_rate': sms_match_rate},
        'email': {'name': 'Email', 'components': ['append', 'send'], 'match_rate': email_match_rate},
    }

    # Digital advertising tactics - read CPMs from database
    digital_tactics_config = {
        'facebook': {'name': 'Facebook/Meta Ads'},
        'ctv': {'name': 'Connected TV (CTV)'},
        'display': {'name': 'Display Ads'},
    }
    # Get CPMs and management fees from price_map (falls back to defaults if not in DB)
    digital_cpms = {}
    for tactic in digital_tactics_config:
        cpm_info = price_map.get((tactic, 'cpm'), {'price': 5.00})
        mgmt_info = price_map.get((tactic, 'management'), {'price': 500.00})
        digital_cpms[tactic] = {
            'name': digital_tactics_config[tactic]['name'],
            'cpm': float(cpm_info['price']),
            'mgmt_fee': float(mgmt_info['price'])
        }

    breakdown = {}
    grand_total = 0

    # Calculate voter contact tactics
    for tactic_key, tactic_info in voter_tactics.items():
        num_rounds = rounds.get(tactic_key, 0)
        if num_rounds == 0:
            continue

        # Apply match rate to get effective voter count for this tactic
        match_rate = tactic_info.get('match_rate', 1.0)
        effective_voters = int(voter_count * match_rate)

        volume_discount = get_volume_discount(tactic_key, effective_voters)
        tactic_total = 0
        components = []

        for comp in tactic_info['components']:
            price_info = price_map.get((tactic_key, comp), {'price': 0, 'is_per_round': 1, 'description': '', 'pricing_model': 'per_unit'})
            base_price = price_info['price']
            unit_price = base_price * (1 - volume_discount)

            if price_info['is_per_round']:
                cost = effective_voters * num_rounds * unit_price
                units = effective_voters * num_rounds
                desc = f"{effective_voters:,} x {num_rounds} x ${unit_price:.3f}"
            else:
                cost = effective_voters * unit_price
                units = effective_voters
                desc = f"{effective_voters:,} x ${unit_price:.3f} (one-time)"

            components.append({
                'name': comp.replace('_', ' ').title(),
                'unit_price': unit_price,
                'base_price': base_price,
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
            'subtotal': tactic_total,
            'volume_discount': volume_discount * 100,
            'match_rate': match_rate * 100,
            'effective_voters': effective_voters
        }
        grand_total += tactic_total

    # Calculate digital advertising tactics (impressions-based)
    digital_tactics = digital.get('tactics', {})
    matched_voters = digital.get('matched_voters', 0)

    for tactic_key, tactic_info in digital_cpms.items():
        impressions_per_voter = digital_tactics.get(tactic_key, 0)
        if impressions_per_voter == 0:
            continue

        cpm = tactic_info['cpm']
        mgmt_fee = tactic_info['mgmt_fee']  # Flat monthly fee from database
        impressions = matched_voters * impressions_per_voter
        ad_spend = (impressions / 1000) * cpm
        tactic_total = ad_spend + mgmt_fee

        breakdown[tactic_key] = {
            'name': tactic_info['name'],
            'impressions_per_voter': impressions_per_voter,
            'matched_voters': matched_voters,
            'total_impressions': impressions,
            'cpm': cpm,
            'components': [
                {
                    'name': 'Ad Spend',
                    'cost': ad_spend,
                    'description': f"{impressions:,} impressions @ ${cpm:.2f} CPM"
                },
                {
                    'name': 'Management Fee',
                    'cost': mgmt_fee,
                    'description': f"Monthly management fee"
                }
            ],
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
# MAP ROUTES
# ============================================================================

@app.route('/map')
def voter_map():
    """Voter map page showing geocoded addresses."""
    return render_template('map.html')


@app.route('/api/map-points')
@csrf.exempt
def api_map_points():
    """Get geocoded voter coordinates."""
    try:
        headers = {'X-API-Key': VOTER_API_KEY} if VOTER_API_KEY else {}
        resp = requests.get(f"{VOTER_API}/api/map-points", headers=headers, timeout=60)
        resp.raise_for_status()
        return Response(resp.content, mimetype='application/json')
    except Exception as e:
        print(f"Map API error: {e}")
        return jsonify({'error': str(e), 'points': []})


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
    volume_tiers = conn.execute('SELECT * FROM volume_tiers ORDER BY tactic, min_qty').fetchall()
    conn.close()

    # Group prices by tactic
    grouped = {}
    for p in prices:
        if p['tactic'] not in grouped:
            grouped[p['tactic']] = []
        grouped[p['tactic']].append(dict(p))

    # Group volume tiers by tactic
    tiers_grouped = {}
    for t in volume_tiers:
        if t['tactic'] not in tiers_grouped:
            tiers_grouped[t['tactic']] = []
        tiers_grouped[t['tactic']].append(dict(t))

    return render_template('admin.html',
                          prices=grouped,
                          volume_tiers=tiers_grouped,
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


@app.route('/admin/update-volume-tiers', methods=['POST'])
@login_required
def update_volume_tiers():
    """Update volume discount tiers."""
    data = request.json or {}

    conn = get_db()
    # Clear existing tiers and insert new ones
    for tactic, tiers in data.items():
        conn.execute('DELETE FROM volume_tiers WHERE tactic = ?', (tactic,))
        for tier in tiers:
            conn.execute('''INSERT INTO volume_tiers (tactic, min_qty, max_qty, discount_percent)
                           VALUES (?, ?, ?, ?)''',
                        (tactic, tier['min'], tier.get('max'), tier['discount']))
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
