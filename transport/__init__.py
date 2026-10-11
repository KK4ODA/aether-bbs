"""
transport/

Transport abstraction layer for the Aether BBS.

Supports multiple modem backends (VARA FM/HF, Soundmodem via AGWPE/KISS)
through a common broker protocol.  Each instance is configured with one
transport type; the BBS session layer is transport-agnostic.
"""
