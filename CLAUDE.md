# Campaign Cost Calculator - Claude Reference

## Server Access

```bash
# SSH to production server
ssh root@138.197.20.97

# App directory
cd /opt/campaign-cost-calculator
```

## Deployment

```bash
# Commit, push, and deploy
git add -A && git commit -m "message" && git push && \
ssh root@138.197.20.97 "cd /opt/campaign-cost-calculator && git pull && systemctl restart campaign-cost-calculator"
```

## Checking Logs

```bash
# Recent logs
ssh root@138.197.20.97 "journalctl -u campaign-cost-calculator -n 50 --no-pager"

# Follow logs live
ssh root@138.197.20.97 "journalctl -u campaign-cost-calculator -f"
```

## Database

Uses SQLite at `/opt/campaign-cost-calculator/calculator.db`

### Tables

- **prices** - Pricing for each tactic/component
  - tactic (direct_mail, sms, email)
  - component (postage, printing, append, send)
  - price (decimal)
  - is_per_round (0 = one-time, 1 = per round)

- **settings** - App settings (key/value)
  - admin_password (hashed)
  - discount_percent
  - company_name
  - notes

- **quotes** - Saved quote history

## Routes

### Public
- `/` - Main calculator
- `/api/voter-count` (POST) - Get voter count with filters
- `/api/calculate` (POST) - Calculate costs
- `/api/save-quote` (POST) - Save quote

### Admin
- `/admin/login` - Login page
- `/admin` - Admin dashboard
- `/admin/update-prices` (POST) - Update pricing
- `/admin/update-settings` (POST) - Update settings
- `/admin/change-password` (POST) - Change password
- `/admin/logout` - Logout

## Dependencies

- Voter File API: http://138.197.36.143:5050
  - `/api/districts` - List districts
  - `/api/district-counts` - Get voter counts with filters
  - `/api/available-elections` - List elections

## Environment Variables

```bash
SECRET_KEY=<random-string>
VOTER_API=http://138.197.36.143:5050
VOTER_API_KEY=<api-key>
```

## Default Pricing

| Tactic | Component | Price | Type |
|--------|-----------|-------|------|
| Direct Mail | Postage | $0.50 | per round |
| Direct Mail | Printing | $0.35 | per round |
| SMS | Append | $0.05 | one-time |
| SMS | Send | $0.025 | per round |
| Email | Append | $0.03 | one-time |
| Email | Send | $0.008 | per round |

## Admin Password

Default: `changeme` (change immediately after first login)

## Tech Stack

- Flask 3.x
- SQLite
- Tailwind CSS (CDN)
- Alpine.js (CDN)
- Flask-WTF (CSRF)
- Flask-Limiter (rate limiting)
