"""Compatibility entry point for older Renpho Garmin Sync installations."""

import sys

import garmin_health_import as _app

if __name__ == "__main__":
    sys.exit(_app.main())
else:
    # Old imports share the same state and functions as the current module.
    sys.modules[__name__] = _app
