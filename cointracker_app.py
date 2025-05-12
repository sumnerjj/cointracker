"""CoinTracker Part 1 — Flask prototype

Features implemented
--------------------
* Add Bitcoin wallet address (validates legacy / P2SH / Bech32) through a simple HTML form.
* Persist addresses in a local SQLite DB (via SQLAlchemy).
* Kick off a Celery background task that fetches current balance & the first page of transactions
  from Blockchair’s public API.
* List view of all wallets with cached balances.
* Detail view for a single wallet with balance + (up to 100) recent transactions.

Prerequisites
-------------
$ pip install flask flask_sqlalchemy celery redis requests
# Make sure redis‑server is running locally.

Running
-------
$ export FLASK_APP=cointracker_app.py
$ flask run               # starts the web server on http://127.0.0.1:5000
$ celery -A cointracker_app.celery worker -l info  # in another terminal
"""

from __future__ import annotations

import os
import re
import json
from datetime import datetime
from pathlib import Path

import requests
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    render_template_string,
    request,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from celery import Celery

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
DATABASE_PATH = BASE_DIR / "cointracker.db"


def _ensure_template_files() -> None:
    """Materialise very small Jinja templates so the app works out of the box."""

    templates: dict[str, str] = {
        "layout.html": """<!doctype html>\n<html lang=\"en\">\n  <head>\n    <meta charset=\"utf-8\">\n    <title>{% block title %}{% endblock %} – CoinTracker</title>\n    <link rel=\"stylesheet\" href=\"https://cdn.jsdelivr.net/npm/water.css@2/out/water.css\">\n  </head>\n  <body>\n    <header><h1><a href={{ url_for('list_wallets') }}>CoinTracker</a></h1></header>\n    {% with messages = get_flashed_messages(with_categories=true) %}\n      {% if messages %}\n        <ul class=\"flashes\">\n        {% for category, message in messages %}\n          <li class={{ category }}>{{ message }}</li>\n        {% endfor %}\n        </ul>\n      {% endif %}\n    {% endwith %}\n    {% block content %}{% endblock %}\n  </body>\n</html>\n""",
        "wallets.html": """{% extends 'layout.html' %}\n{% block title %}Wallets{% endblock %}\n{% block content %}\n  <a href={{ url_for('add_wallet') }}>&#x2795; Add wallet</a>\n  <table>\n    <tr><th>Address</th><th>Balance&nbsp;(BTC)</th><th>Last&nbsp;synced</th></tr>\n    {% for w in wallets %}\n      <tr>\n        <td><a href={{ url_for('detail_wallet', wallet_id=w.id) }}>{{ w.address }}</a></td>\n        <td>{{ '%.8f'|format(w.balance_sat/1e8) }}</td>\n        <td>{{ w.last_synced_at or '—' }}</td>\n      </tr>\n    {% endfor %}\n  </table>\n{% endblock %}\n""",
        "add_wallet.html": """{% extends 'layout.html' %}\n{% block title %}Add wallet{% endblock %}\n{% block content %}\n  <h2>Add Bitcoin wallet</h2>\n  <form method=post>\n    <label for=address>Address</label>\n    <input name=address id=address size=60 required autofocus>\n    <button type=submit>Add</button>\n  </form>\n{% endblock %}\n""",
        "wallet_detail.html": """{% extends 'layout.html' %}\n{% block title %}Wallet {{ wallet.id }}{% endblock %}\n{% block content %}\n  <h2>{{ wallet.address }}</h2>\n  <p>Balance: {{ '%.8f'|format(wallet.balance_sat/1e8) }} BTC</p>\n  <p>Last synced: {{ wallet.last_synced_at or 'never' }}</p>\n  <h3>Transactions (latest {{ txs|length }})</h3>\n  <ul>\n    {% for t in txs %}\n      <li><code>{{ t.tx_hash }}</code></li>\n    {% endfor %}\n  </ul>\n{% endblock %}\n""",
    }

    TEMPLATES_DIR.mkdir(exist_ok=True)
    for name, content in templates.items():
        fp = TEMPLATES_DIR / name
        if not fp.exists():
            fp.write_text(content, encoding="utf-8")


_ensure_template_files()

# ---------------------------------------------------------------------------
# Flask & SQLAlchemy setup
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder=str(TEMPLATES_DIR))
app.config.update(
    SQLALCHEMY_DATABASE_URI=f"sqlite:///{DATABASE_PATH}",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SECRET_KEY=os.getenv("SECRET_KEY", "dev"),
)

db = SQLAlchemy(app)


class Wallet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    address = db.Column(db.String(100), unique=True, nullable=False)
    label = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    balance_sat = db.Column(db.BigInteger, default=0)
    last_synced_at = db.Column(db.DateTime)


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(db.Integer, db.ForeignKey("wallet.id"), nullable=False)
    tx_hash = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime)
    direction = db.Column(db.String(4))  # IN / OUT (optional in Part 1)
    amount_sat = db.Column(db.BigInteger)
    fee_sat = db.Column(db.BigInteger)
    height = db.Column(db.Integer)

    wallet = db.relationship("Wallet", backref=db.backref("transactions", lazy=True))


with app.app_context():
    db.create_all()

# ---------------------------------------------------------------------------
# Celery setup (Redis broker/back‑end by default)
# ---------------------------------------------------------------------------

def make_celery(flask_app: Flask) -> Celery:
    celery_obj = Celery(
        flask_app.import_name,
        broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
        backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0"),
    )
    celery_obj.conf.update(flask_app.config)

    class ContextTask(celery_obj.Task):
        def __call__(self, *args, **kwargs):
            with flask_app.app_context():
                return super().__call__(*args, **kwargs)

    celery_obj.Task = ContextTask
    return celery_obj


celery: Celery = make_celery(app)

# ---------------------------------------------------------------------------
# Bitcoin address validation helpers
# ---------------------------------------------------------------------------

# Very lightweight regex for common cases:
ADDRESS_REGEX = re.compile(r"^(bc1[0-9a-z]{25,39}|[13][a-km-zA-HJ-NP-Z0-9]{25,34})$")

def is_valid_address(addr: str) -> bool:
    """Return True if *addr* looks like a Bitcoin main‑net address."""
    return bool(ADDRESS_REGEX.fullmatch(addr))

# ---------------------------------------------------------------------------
# Celery task: initial sync (balance + first 100 tx hashes for demo)
# ---------------------------------------------------------------------------

BLOCKCHAIR_API_URL = "https://api.blockchair.com/bitcoin/dashboards/address/{address}"


@celery.task(name="sync_wallet")
def sync_wallet(wallet_id: int) -> None:
    wallet = Wallet.query.get(wallet_id)
    if not wallet:
        return  # vanished / removed

    try:
        resp = requests.get(
            BLOCKCHAIR_API_URL.format(address=wallet.address),
            params={"limit": 100, "offset": 0},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        app.logger.info('payload {}'.format(payload))
        data = payload.get("data", {}).get(wallet.address, {})

        # Update balance (satoshis)
        wallet.balance_sat = data.get("address", {}).get("balance", 0)
        wallet.last_synced_at = datetime.utcnow()
        db.session.add(wallet)

        # Insert basic transaction hashes (direction / amount not parsed in Part 1)
        for tx_hash in data.get("transactions", []):
            if not Transaction.query.filter_by(wallet_id=wallet.id, tx_hash=tx_hash).first():
                db.session.add(Transaction(wallet_id=wallet.id, tx_hash=tx_hash))

        db.session.commit()
    except Exception:  # noqa: BLE001
        app.logger.exception("Failed to sync wallet %s", wallet.address)
        db.session.rollback()

# ---------------------------------------------------------------------------
# Web routes / views
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("list_wallets"))


@app.route("/wallets")
def list_wallets():
    wallets = Wallet.query.order_by(Wallet.created_at.desc()).all()
    return render_template("wallets.html", wallets=wallets)


@app.route("/wallets/add", methods=["GET", "POST"])
def add_wallet():
    if request.method == "POST":
        address = request.form.get("address", "").strip()
        if not is_valid_address(address):
            flash("Invalid Bitcoin address", "error")
            return redirect(url_for("add_wallet"))

        existing = Wallet.query.filter_by(address=address).first()
        if existing:
            flash("Address already exists", "warning")
            return redirect(url_for("detail_wallet", wallet_id=existing.id))

        wallet = Wallet(address=address)
        db.session.add(wallet)
        db.session.commit()

        # Kick off background sync
        sync_wallet.delay(wallet.id)

        flash("Wallet added – initial sync scheduled", "success")
        return redirect(url_for("detail_wallet", wallet_id=wallet.id))

    return render_template("add_wallet.html")


@app.route("/wallets/<int:wallet_id>")
def detail_wallet(wallet_id: int):
    wallet = Wallet.query.get_or_404(wallet_id)
    txs = (
        Transaction.query.filter_by(wallet_id=wallet.id)
        .order_by(Transaction.id.desc())
        .limit(100)
        .all()
    )
    return render_template("wallet_detail.html", wallet=wallet, txs=txs)


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
