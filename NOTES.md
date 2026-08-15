# Notes

## Known limitations / future work

- **EDGAR cache has no TTL.** `EdgarClient`'s disk cache
  (`src/data/edgar_client.py`) is presence-based: once a URL's response is
  written to `data/cache/`, it's served from disk indefinitely with no
  expiration or invalidation. Fine for development, but before relying on
  this for real use it needs a TTL (e.g. re-fetch if the cache file is older
  than N hours/days) or an explicit invalidation mechanism — otherwise stale
  data (like an outdated ticker-to-CIK mapping) can persist silently forever.

- **Rate limiter doesn't coordinate across instances or processes.**
  `EdgarClient._throttle` (`src/data/edgar_client.py`) tracks
  `_last_request_time` per instance, in-process, with no locking. It keeps a
  single `EdgarClient` under 10 requests/second, but multiple instances or
  processes hitting EDGAR concurrently could collectively exceed the limit.
  Fine at current scope, but would need rethinking (e.g. a shared/external
  rate limiter) if requests are parallelized.
