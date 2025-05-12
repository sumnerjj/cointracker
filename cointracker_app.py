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
import time
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


def _ensure_template_files() -> None:  # noqa: C901 – long but simple
    templates: dict[str, str] = {
        "layout.html": """<!doctype html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\">\n  <title>{% block title %}{% endblock %} – CoinTracker</title>\n  <link rel=\"stylesheet\" href=\"https://cdn.jsdelivr.net/npm/water.css@2/out/water.css\">\n</head>\n<body>\n<header><h1><a href={{ url_for('list_wallets') }}>CoinTracker</a></h1></header>\n{% with messages = get_flashed_messages(with_categories=true) %}{% if messages %}<ul class=flashes>{% for c,m in messages %}<li class={{c}}>{{m}}</li>{% endfor %}</ul>{% endif %}{% endwith %}\n{% block content %}{% endblock %}\n</body>\n</html>\n""",
        "wallets.html": """{% extends 'layout.html' %}{% block title %}Wallets{% endblock %}{% block content %}\n<a href={{ url_for('add_wallet') }}>&#x2795; Add wallet</a>\n<table>\n<tr><th>Address</th><th>Balance (BTC)</th><th>Last synced</th><th>Status</th></tr>\n{% for w in wallets %}<tr>\n<td><a href={{ url_for('detail_wallet', wallet_id=w.id) }}>{{ w.address }}</a></td>\n<td>{{ '%.8f'|format(w.balance_sat/1e8) }}</td>\n<td>{{ w.last_synced_at or '—' }}</td>\n<td>{{ w.last_sync.status if w.last_sync else '—' }}</td>\n</tr>{% endfor %}\n</table>{% endblock %}\n""",
        "add_wallet.html": """{% extends 'layout.html' %}{% block title %}Add wallet{% endblock %}{% block content %}\n<h2>Add Bitcoin wallet</h2>\n<form method=post><label for=address>Address</label><input name=address id=address size=60 required autofocus><button type=submit>Add</button></form>{% endblock %}\n""",
        "wallet_detail.html": """{% extends 'layout.html' %}{% block title %}Wallet{% endblock %}{% block content %}\n<h2>{{ wallet.address }}</h2>\n<p>Balance: {{ '%.8f'|format(wallet.balance_sat/1e8) }} BTC</p>\n{% if sync %}<p>Last sync: {{ sync.started_at }} → {{ sync.ended_at or 'running' }} (status: {{ sync.status }})</p>\n{% if sync.status == 'in_progress' %}<div style='padding: 0.5em 1em; background: #fffbe6; color: #a67c00; border-left: 4px solid #ffc107; margin: 1em 0;'>🔄 Sync is currently in progress.<br>This may take several minutes for large wallets. Refresh the page to check progress.</div>{% endif %}{% endif %}\n<form action={{ url_for('trigger_sync', wallet_id=wallet.id) }} method=post><button type=submit>&#x21bb; Re‑sync</button></form>\n<h3>Latest {{ txs|length }} transactions</h3><ul>{% for t in txs %}<li><code>{{ t.tx_hash }}</code></li>{% endfor %}</ul>{% endblock %}\n""",
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


class SyncRun(db.Model):
    """Tracks a single end‑to‑end sync of one wallet."""

    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(db.Integer, db.ForeignKey("wallet.id"), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime)
    status = db.Column(db.String(16), default="in_progress")  # in_progress/completed/error
    current_offset = db.Column(db.Integer, default=0)
    error_message = db.Column(db.String(255))
    is_latest = db.Column(db.Boolean, default=True)  # Only one TRUE per wallet

    wallet = db.relationship("Wallet", backref=db.backref("sync_runs", lazy=True))


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
PAGE_SIZE = 100
MAX_RETRIES = 5
RATE_LIMIT_SLEEP = 1.0  # seconds after 429


def _request_with_retry(url: str, params: dict) -> dict:
    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            resp = requests.get(url, params=params, timeout=20)
            if resp.status_code == 429:  # rate limit
                sleep_for = int(resp.headers.get("Retry-After", RATE_LIMIT_SLEEP))
                time.sleep(sleep_for)
                attempt += 1
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:  # network / server errors
            attempt += 1
            backoff = 2**attempt
            time.sleep(backoff)
            if attempt >= MAX_RETRIES:
                raise exc
    raise RuntimeError("unreachable retry loop")


@celery.task(name="sync_wallet")
def sync_wallet(wallet_id: int) -> None:  # noqa: C901 – functionally long
    wallet = Wallet.query.get(wallet_id)
    if not wallet:
        return

    app.logger.info(f"Starting sync for wallet {wallet.address} (id={wallet.id})")

    current_sync: SyncRun | None = (
        SyncRun.query.filter_by(wallet_id=wallet.id, is_latest=True).first()
    )
    if current_sync and current_sync.status == "in_progress":
        sync_run = current_sync
        app.logger.info(f"Resuming existing sync at offset {sync_run.current_offset}")
    else:
        if current_sync:
            current_sync.is_latest = False
            db.session.add(current_sync)
        sync_run = SyncRun(wallet_id=wallet.id, current_offset=0)
        db.session.add(sync_run)
        db.session.commit()
        app.logger.info("Created new sync run")

    offset = sync_run.current_offset or 0
    more_data = True

    try:
        while more_data:
            app.logger.info(f"Fetching transactions from offset {offset}")
            params = {"limit": PAGE_SIZE, "offset": offset}
            data = _request_with_retry(
                BLOCKCHAIR_API_URL.format(address=wallet.address), params
            )
            entry = data.get("data", {}).get(wallet.address, {})
            address_meta = entry.get("address", {})

            wallet.balance_sat = address_meta.get("balance", wallet.balance_sat)
            db.session.add(wallet)

            tx_hashes: list[str] = entry.get("transactions", [])
            app.logger.info(f"Retrieved {len(tx_hashes)} transactions")
            if not tx_hashes:
                more_data = False
                break

            inserted = 0
            for txh in tx_hashes:
                if not Transaction.query.filter_by(wallet_id=wallet.id, tx_hash=txh).first():
                    db.session.add(Transaction(wallet_id=wallet.id, tx_hash=txh))
                    inserted += 1

            app.logger.info(f"Inserted {inserted} new transactions")

            offset += PAGE_SIZE
            sync_run.current_offset = offset
            db.session.add(sync_run)
            db.session.commit()

            if len(tx_hashes) < PAGE_SIZE:
                more_data = False

        sync_run.status = "completed"
        wallet.last_synced_at = datetime.utcnow()
        sync_run.ended_at = datetime.utcnow()
        db.session.add(sync_run)
        db.session.commit()
        app.logger.info(f"Sync completed for wallet {wallet.address}")

    except Exception as exc:  # noqa: BLE001
        app.logger.exception("Sync failed for wallet %s", wallet.address)
        db.session.rollback()
        sync_run.status = "error"
        sync_run.error_message = str(exc)[:250]
        sync_run.ended_at = datetime.utcnow()
        db.session.add(sync_run)
        db.session.commit()
        app.logger.error(f"Sync failed: {exc}")
        raise



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
    sync = SyncRun.query.filter_by(wallet_id=wallet.id, is_latest=True).first()
    txs = (
        Transaction.query.filter_by(wallet_id=wallet.id)
        .order_by(Transaction.id.desc())
        .limit(100)
        .all()
    )
    return render_template("wallet_detail.html", wallet=wallet, sync=sync, txs=txs)


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
