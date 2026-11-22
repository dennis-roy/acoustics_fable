"""Signal processing for the microphone stream: the decimation filter."""

from .decimate import DEFAULT_DECIMATION, FilterSpec, Samples, decimate_bits, decimate_signal, filters, get_filter

__all__ = ["DEFAULT_DECIMATION", "FilterSpec", "Samples", "decimate_bits", "decimate_signal", "filters", "get_filter"]
