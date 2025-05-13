# CoinTracker – Design Documentation

## Table of Contents

* [API Documentation](#api-documentation)
* [Architecture Overview](#architecture-overview)
* [Key Design Decisions](#key-design-decisions)
* [Future Improvements](#future-improvements)
* [Production Considerations](#production-considerations)

  * [Multi-Chain Support](#multi-chain-support)
  * [Scalability](#scalability)
  * [Production Readiness](#production-readiness)

---

## API Documentation

**Endpoints**

| Method | Route                         | Description                               |
| ------ | ----------------------------- | ----------------------------------------- |
| GET    | `/wallets`                    | List all wallets                          |
| GET    | `/wallets/add`                | Render form to add a wallet               |
| POST   | `/wallets/add`                | Add a wallet and trigger initial sync     |
| GET    | `/wallets/<wallet_id>`        | View details for a specific wallet        |
| POST   | `/wallets/<wallet_id>/resync` | Trigger a resync for the wallet           |
| POST   | `/wallets/<wallet_id>/delete` | Delete the wallet and all associated data |

These routes are rendered with HTML and not RESTful JSON APIs. In a future version, `/api/...` endpoints could be added for programmatic access.

---

## Architecture Overview

### Components

* **Flask App (Monolith)**: Handles routing, rendering, and DB access.
* **SQLAlchemy Models**: Maps `Wallet`, `Transaction`, and `SyncRun` to a relational database.
* **Celery Worker**: Background sync process for fetching blockchain data from Blockchair API.
* **SQLite DB**: Used for persistence (simple, file-based, not for production scale).
* **Blockchair API**: External blockchain data source.

### Data Flow

1. User adds a wallet via web form.
2. A `Wallet` record is created; a Celery task is queued to sync it.
3. The Celery worker fetches transaction data and stores it in `Transaction` and updates `Wallet`.
4. Users can resync or view progress/status.

---

## Key Design Decisions

* **Task Queue (Celery)**: Used for non-blocking sync operations.
* **Stateless Sync Logic**: Syncs can resume mid-process using `current_offset`.
* **Single Table per Entity**: Keeps schema clear; no polymorphism yet.
* **Address Validation with Regex**: Simple but effective for filtering out invalid inputs.

---

## Future Improvements

1. **REST API Support**: Add `/api/wallets`, `/api/transactions` for integration.
2. **Pagination**: Add pagination to wallet and transaction views.
3. **Auth System**: Introduce user accounts and per-user wallets.
4. **Rate Limit Management**: Smarter handling of API rate limits and caching responses.
5. **Retry Management**: Persist retry state across restarts; make retry intervals configurable.
6. **Wallet Labeling**: Enable user-custom labels and tagging.
7. **Error Reporting**: Integrate Sentry or similar for async task failures.

---

## Production Considerations

### Multi-Chain Support

**Design Goals**

* Easily support Ethereum, Litecoin, Dogecoin, etc.
* Isolate chain-specific logic.

**Approach**

* Introduce a `Blockchain` abstraction layer:

  ```python
  class Blockchain:
      def sync_wallet(self, wallet: Wallet): ...
      def validate_address(self, address: str) -> bool: ...
  ```
* Create implementations: `BitcoinChain`, `EthereumChain`, etc.
* Add a `chain` column to `Wallet` (e.g., `"bitcoin"`, `"ethereum"`).

---

### Scalability

**Challenges**

* Power users with thousands of wallets
* Wallets with 100K+ transactions
* Concurrent sync jobs

**Strategies**

* **Multiple wallet upload**: Support uploading a spreadsheet of wallets
* **Sync complete notification**: Send user notification on sync completion
* **Database**: Use PostgreSQL or another production-grade RDBMS.
* **Indexing**: Add indexes on `wallet_id`, `tx_hash`, `timestamp`.
* **Message Queues**: use dedicated queue topics assigned by wallet or user id
* **Sharding**: Shard DB by user id
* **Caching**: Use Redis/Memcached for frequently accessed wallet summaries.
* **Background Aggregates**: Nightly jobs to compute and cache balance and summary metrics.

---

### Production Readiness

**Observability**

* **Health Checks**: Monitor sync job queue length, failed syncs, error rates, cpu, memory
* **Monitoring**: Use Prometheus + Grafana or DataDog.
* **Error Tracking**: Integrate with Sentry or Rollbar to capture errors in Flask and Celery.
* **Rate Limit Alerts**: Alert on repeated 429s or API quota exhaustion.

**Deployment**

* Use a real API key for better rate limits on the Blockchair API
* Store secrets in environment variables or secret manager (e.g., Vault)

### Use of AI note:
I used 4o to write a lot of the boilerplate and CRUD code, as well as first draft documentation
