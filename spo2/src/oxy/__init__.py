"""Reflective PPG pulse oximetry on Raspberry Pi + MAX30102.

Pipeline:  MAX30102 FIFO -> 5 s window -> band-pass -> R ratio -> SpO2
                                        -> peak detection -> heart rate
                                        -> perfusion index + signal quality

The package is deliberately importable without any hardware library so the
simulator, the replay path and the self-test all run on a normal laptop.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
