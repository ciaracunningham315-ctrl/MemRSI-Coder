# Inventory service

This is a small in-process JSON API. Its public contract is versioned in
[docs/api-contract.md](docs/api-contract.md). Routes are split by business concern;
shared state belongs to one API instance. No database or web server is required.

Run basic checks with `python -m unittest discover -s tests`.
These checks are a smoke test; consult the contract for supported inputs and
observable state/error behavior. Internal organization is not an API guarantee.
