"""Halcyon: an open HF data modem (OFDM + LDPC + adaptive coding)."""

from .bits import crc16, crc16_ok
from .ldpc import LDPCCode, code_for
from .ofdm import Phy
from .qam import qam

__all__ = ["crc16", "crc16_ok", "LDPCCode", "code_for", "Phy", "qam"]
__version__ = "0.1.1"
