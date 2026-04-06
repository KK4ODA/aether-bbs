"""
vara/mux.py

Backwards-compatibility shim.

VARAFMMux has been superseded by TNCBroker in vara/broker.py.
All existing imports of VARAFMMux and SessionHandler continue to work unchanged.
"""

from vara.broker import TNCBroker as VARAFMMux  # noqa: F401  (re-export)
from vara.broker import SessionHandler           # noqa: F401  (re-export)

__all__ = ["VARAFMMux", "SessionHandler"]
