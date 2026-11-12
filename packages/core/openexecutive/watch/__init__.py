"""Watch jobs: unattended ERPNext runs defined in code (phase 0's S5).

A watch job reads a fixed set of ERPNext data, applies rules in code and
leaves the principal notices. It never runs the Executive's tool loop.

- ``registry``: the jobs, in code only (``WatchJob``, ``WatchRead``,
  ``CardSpec``) and the argument shapes oe_connector holds them to;
- ``settings``: each job's row in ``watch_jobs`` (on/off, settings, who
  configured it), changed by the principal alone (``PUT /watch/jobs/{id}``);
- ``runner``: once a day per job, started by the scheduler's tick, each read
  under ``erpnext_caller.watch_run`` and signed as the ``watch`` caller.
"""
