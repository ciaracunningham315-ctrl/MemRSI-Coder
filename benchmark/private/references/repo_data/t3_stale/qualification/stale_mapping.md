# T3 stage transition evidence

The fixed T1 reference repair changes only ledgerflow/sales.py; the T2 reference repair changes only ledgerflow/shipments.py. T3 preserves the T1 sales implementation and ledgerflow/money.py parse_major helper. The T2 shipment implementation is moved to ledgerflow/dispatch.py as format_manifest; ledgerflow/shipments.py retains the import alias needed by callers but no longer defines prepare_manifest as an AST function. Thus an experience anchored to editing the old implementation file/symbol needs re-evaluation, while the major-unit helper path and symbol remain usable for the legacy flows.

This is a deterministic comparison of authored reference repairs and frozen code snapshots. It does not claim that a model-generated Agent history has been collected or that MemoryService applicability has been exercised for repo_data.
