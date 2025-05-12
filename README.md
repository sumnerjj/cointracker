# CoinTracker Flask Prototype

This is a prototype Bitcoin wallet tracker built with Flask. It allows users to:

* Add Bitcoin wallet addresses (with format validation)
* View balances and basic transaction history
* Automatically fetch wallet data via a background Celery task

## Features

* Address validation for legacy, P2SH, and Bech32 formats
* Uses Blockchair API to retrieve balance and recent transactions
* SQLite for persistent local storage
* Redis + Celery for asynchronous background processing
* Basic UI built with server-rendered templates

## Prerequisites

Make sure the following are installed:

* Python 3.8+
* Redis server (for Celery backend)

Install Python dependencies:

```bash
pip install flask flask_sqlalchemy celery redis requests
```

## Running the App

Start Redis server if it's not already running:

```bash
redis-server
```

Start the Flask web server:

```bash
FLASK_APP=cointracker_app.py flask run
```

Start the Celery worker (in a separate terminal):

```bash
celery -A cointracker_app.celery worker -l info  -P solo
```

Access the application at: [http://127.0.0.1:5000](http://127.0.0.1:5000)

## File Structure

```
cointracker_app.py   # Main Flask app and Celery integration
cointracker.db       # SQLite database (auto-created)
```

