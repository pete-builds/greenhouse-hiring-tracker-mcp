"""Hiring Hub: one job store across every public applicant tracking system.

Connectors normalise each ATS's public job board into `models.Job`. `store`
keeps them in SQLite with first-seen / removed lifecycle, `scoring` ranks them
against a job seeker's profile, and `store` also carries the application
pipeline so search and follow-up live in the same place.
"""

__version__ = "1.0.0"
