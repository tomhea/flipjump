"""
the --io device selector.

The CLI picks ONE complete IODevice (a "mode"); each device owns all of its channels (and
its window, if any). Adding a new mode is one IO_MODES entry plus its device class.

an --io mode string is the mode name, optionally followed by whitespace-separated parameters
that are passed to the mode's factory (no current mode takes parameters).

modes:
  standard - terminal input/output (the default).
  pc       - an interactive window: live keyboard input + a scaled 256-color screen.

headless/scripted devices (e.g. scripted keys + PNG frames, or a plain InMemoryScreen) are
built programmatically and passed straight to fjm_run.run(io_device=...) - e.g.
PcIO.headless(events_file, frames_dir) - they are not --io modes.
"""

from typing import Callable, Dict, List, Tuple

from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.interpreter.io_devices.StandardIO import StandardIO
from flipjump.utils.exceptions import IODeviceException


def _require_no_parameters(mode_name: str, parameters: List[str]) -> None:
    if parameters:
        raise IODeviceException(f'the {mode_name!r} --io mode takes no parameters, got: {" ".join(parameters)!r}')


def _make_standard(parameters: List[str]) -> IODevice:
    _require_no_parameters('standard', parameters)
    return StandardIO(True)


def _make_pc(parameters: List[str]) -> IODevice:
    _require_no_parameters('pc', parameters)
    # imported lazily: the interactive window needs pygame-ce (the optional `io` extra)
    from flipjump.interpreter.io_devices.pygame_window import PcIO

    return PcIO.interactive()


# the --io modes - extend with one entry (+ its device class) to add a new complete device.
# each factory gets the mode's parameter list (the whitespace-split tail of the --io string).
IO_MODES: Dict[str, Callable[[List[str]], IODevice]] = {
    'standard': _make_standard,
    'pc': _make_pc,
}


def split_io_mode(io_mode: str) -> Tuple[str, List[str]]:
    """split an --io mode string into (mode_name, parameters), on whitespace."""
    parts = io_mode.split()
    return (parts[0] if parts else '', parts[1:])


def make_io_device(io_mode: str) -> IODevice:
    """build the complete io-device for the given --io mode string: the mode name,
    optionally followed by whitespace-separated parameters for the mode's factory."""
    mode_name, parameters = split_io_mode(io_mode)
    if not mode_name:
        raise IODeviceException(f'empty --io mode. supported: {", ".join(IO_MODES)}')
    factory = IO_MODES.get(mode_name)
    if factory is None:
        raise IODeviceException(f'unknown --io mode: {mode_name!r}. supported: {", ".join(IO_MODES)}')
    return factory(parameters)
