"""The agent worker: conversation loop, models, jobs, reflection."""

import os

# pydantic-ai prints a multi-line ASCII banner on first Agent construction,
# advertising its hosted observability product. Harmless in a terminal,
# noise in a log file the supervisor captures on every worker start.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
