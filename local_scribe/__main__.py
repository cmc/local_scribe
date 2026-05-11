"""``python -m local_scribe`` entry point.

Delegates to ``local_scribe.cli.__main__`` so the dispatcher's argparse
plumbing can live alongside the rest of the CLI under ``cli/``. This
file exists purely so the top-level ``python -m local_scribe …`` form
works the way operators expect.
"""

from __future__ import annotations

import sys

from local_scribe.cli.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
