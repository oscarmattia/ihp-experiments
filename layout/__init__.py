"""Physical layout generation and signoff for IHP SG13G2.

Devices are placed from the foundry PCells in the PDK's own
``sg13g2_pycell_lib``; gdsfactory is used for composition and electrical
routing only. Every stage is gated on the PDK's DRC, LVS and extraction
decks rather than on our own geometry assumptions.
"""

__all__ = ["common", "devices", "blocks"]
