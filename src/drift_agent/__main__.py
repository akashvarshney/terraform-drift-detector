"""Enable `python -m drift_agent` as required by the design document."""

from drift_agent.main import main

raise SystemExit(main())
