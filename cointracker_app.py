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

    @property
    def last_sync(self):  # noqa: D401 – simple property
        """Return the most recent SyncRun (or *None*)."""
        return (
            SyncRun.query.filter_by(wallet_id=self.id)
            .order_by(SyncRun.started_at.desc())
            .first()
        )


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
    """Tracks one end‑to‑end sync for a wallet."""

    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(db.Integer, db.ForeignKey("wallet.id"), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime)
    status = db.Column(db.String(16), default="in_progress")  # in_progress/completed/error
    current_offset = db.Column(db.Integer, default=0)
    error_message = db.Column(db.String(255))

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

ADDRESS_REGEX = re.compile(r"^(bc1[0-9a-z]{25,39}|[13][a-km-zA-HJ-NP-Z0-9]{25,34})$")

def is_valid_address(addr: str) -> bool:
    return bool(ADDRESS_REGEX.fullmatch(addr))

# ---------------------------------------------------------------------------
# Celery task: sync wallet end‑to‑end (pagination)
# ---------------------------------------------------------------------------

BLOCKCHAIR_API_URL = "https://api.blockchair.com/bitcoin/dashboards/address/{address}"
PAGE_SIZE = 100
MAX_RETRIES = 5
RATE_LIMIT_SLEEP = 1.0  # seconds to wait after 429


def _request_with_retry(url: str, params: dict) -> dict:
    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            resp = requests.get(url, params=params, timeout=20)
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", RATE_LIMIT_SLEEP)))
                attempt += 1
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            attempt += 1
            time.sleep(2 ** attempt)
    raise RuntimeError("Blockchair request failed after retries")


@celery.task(name="sync_wallet")
def sync_wallet(wallet_id: int) -> None:  # noqa: C901 – long but straightforward
    wallet = Wallet.query.get(wallet_id)
    if not wallet:
        return

    # Resume if an in‑progress run exists; else start a new one
    current_sync = (
        SyncRun.query.filter_by(wallet_id=wallet.id)
        .order_by(SyncRun.started_at.desc())
        .first()
    )

    if current_sync and current_sync.status == "in_progress":
        sync_run = current_sync
    else:
        sync_run = SyncRun(wallet_id=wallet.id)
        db.session.add(sync_run)
        db.session.commit()

    offset = sync_run.current_offset or 0
    more_data = True

    try:
        while more_data:
            params = {"limit": PAGE_SIZE, "offset": offset}
            app.logger.info(f"Fetching transactions from offset {offset}")
            data = _request_with_retry(BLOCKCHAIR_API_URL.format(address=wallet.address), params)
            entry = data.get("data", {}).get(wallet.address, {})
            addr_meta = entry.get("address", {})

            # Update balance
            wallet.balance_sat = addr_meta.get("balance", wallet.balance_sat)
            wallet.last_synced_at = datetime.utcnow()
            db.session.add(wallet)

            hashes = entry.get("transactions", [])
            if not hashes:
                break

            for txh in hashes:
                if not Transaction.query.filter_by(wallet_id=wallet.id, tx_hash=txh).first():
                    db.session.add(Transaction(wallet_id=wallet.id, tx_hash=txh))

            offset += PAGE_SIZE
            sync_run.current_offset = offset
            db.session.add(sync_run)
            db.session.commit()

            if len(hashes) < PAGE_SIZE:
                more_data = False

        sync_run.status = "completed"
        sync_run.ended_at = datetime.utcnow()
        db.session.add(sync_run)
        db.session.commit()

    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        sync_run.status = "error"
        sync_run.error_message = str(exc)[:250]
        sync_run.ended_at = datetime.utcnow()
        db.session.add(sync_run)
        db.session.commit()
        raise


# ---------------------------------------------------------------------------
# Web routes / views
# ---------------------------------------------------------------------------

@app.route("/")
def index() -> str:
    return redirect(url_for("list_wallets"))


@app.route("/wallets/")
def list_wallets():  # type: ignore[valid-type]
    wallets = Wallet.query.order_by(Wallet.created_at.desc()).all()
    return render_template("wallets.html", wallets=wallets)


@app.route("/wallets/add/", methods=["GET", "POST"])
def add_wallet():  # type: ignore[valid-type]
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
        sync_wallet.delay(wallet.id)
        flash("Wallet added – initial sync scheduled", "success")
        return redirect(url_for("detail_wallet", wallet_id=wallet.id))

    return render_template("add_wallet.html")


@app.route("/wallets/<int:wallet_id>/")
def detail_wallet(wallet_id: int):  # type: ignore[valid-type]
    wallet = Wallet.query.get_or_404(wallet_id)
    sync = (
        SyncRun.query.filter_by(wallet_id=wallet.id)
        .order_by(SyncRun.started_at.desc())
        .first()
    )
    txs = (
        Transaction.query.filter_by(wallet_id=wallet.id)
        .order_by(Transaction.id.desc())
        .limit(100)
        .all()
    )
    return render_template("wallet_detail.html", wallet=wallet, sync=sync, txs=txs)


@app.route("/wallets/<int:wallet_id>/delete", methods=["POST"])
def delete_wallet(wallet_id: int):
    wallet = Wallet.query.get_or_404(wallet_id)

    # Delete associated transactions and sync runs
    Transaction.query.filter_by(wallet_id=wallet.id).delete()
    SyncRun.query.filter_by(wallet_id=wallet.id).delete()
    db.session.delete(wallet)
    db.session.commit()

    flash("Wallet and associated data deleted", "success")
    return redirect(url_for("list_wallets"))


@app.route("/wallets/<int:wallet_id>/resync", methods=["POST"])
def trigger_sync(wallet_id: int):  # type: ignore[valid-type]
    sync_wallet.delay(wallet_id)
    flash("Re‑sync triggered", "success")
    return redirect(url_for("detail_wallet", wallet_id=wallet_id))


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
