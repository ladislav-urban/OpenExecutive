"""Watch jobs: unattended ERPNext runs defined in code (phase 0's S5).

A watch job reads a fixed set of ERPNext data, applies rules in code and
leaves notices and draft cards. It never runs the Executive's tool loop.

- ``registry``: the jobs, in code only (``WatchJob``, ``WatchRead``,
  ``CardSpec``, ``DraftSpec``) and the argument shapes oe_connector holds
  them to;
- ``settings``: each job's row in ``watch_jobs`` (on/off, settings, who
  configured it, its reviewer), changed by the principal alone
  (``PUT /watch/jobs/{id}``);
- ``runner``: once a day per job, started by the scheduler's tick, each read
  under ``erpnext_caller.watch_run`` and signed as the ``watch`` caller;
- ``cards``: who a run goes to (the job's reviewer, only on oe_connector's
  word that it read as the job's own user; else the principal, notices only)
  and its draft cards, checked by ERPNext as the reviewer;
- ``jobs``: the jobs themselves (``collections``).
"""
