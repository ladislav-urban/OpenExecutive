"""The watch jobs' own code. Each production job is listed in
``watch.registry._LISTED``; ``_example`` is listed by the tests alone.

The registry is imported first, whichever module is asked for: it imports
each listed job at its end, once everything a job uses is defined, so a job
module imported on its own still finds the registry whole.
"""
from openexecutive.watch import registry  # noqa: F401
