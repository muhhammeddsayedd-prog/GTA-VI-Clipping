"""External API clients. Every call to a third-party service goes through a client in this package, with
timeouts, retries (exponential backoff, max 3) and structured logging; each client has a fake in
`fakes.py` for tests.
"""
