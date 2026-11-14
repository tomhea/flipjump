"""
The native (C) engine's memory, for code that drives the engine itself: it loads the words, runs them, and may
reset the memory and run it again. `flipjump.run` does all of this for an .fjm file.

    from flipjump import native

    memory = native.Memory(64)  # the memory width, up to 64
    memory.add_segment(0, 8)  # the segment's first word, and its length in words
    memory.set_words(0, [256, 0])  # one op: it flips bit 256, and jumps to itself
    cause, op_count, *_ = memory.run(io_device.read_bit, io_device.write_bit, IOReadOnEOF)
    assert cause == native.TERM_LOOPING

Importing this module raises an ImportError where the native engine is not built.
"""

from flipjump.interpreter.fjm_run import _fjcore

if _fjcore is None:
    raise ImportError('the native engine is not built in this flipjump install')

__all__ = ['Memory', 'TERM_EOF', 'TERM_LOOPING', 'TERM_MEMORY_ERROR', 'TERM_NULL_IP', 'cpu_calibrate']

Memory = _fjcore.Memory
cpu_calibrate = _fjcore.cpu_calibrate

# what ended a run: the first value that Memory.run returns
TERM_LOOPING: int = _fjcore.TERM_LOOPING
TERM_EOF: int = _fjcore.TERM_EOF
TERM_NULL_IP: int = _fjcore.TERM_NULL_IP
TERM_MEMORY_ERROR: int = _fjcore.TERM_MEMORY_ERROR
