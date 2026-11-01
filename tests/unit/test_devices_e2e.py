"""
end-to-end tests for the io-devices: an .fj program drives the InMemoryScreen over the output
stream (golden frame-hash + PNG output), a scripted keyboard replays deterministically into
an .fj polling loop, and the complete PcIO device (screen + keyboard together) runs a program.
"""

import contextlib
import hashlib
from pathlib import Path
from typing import Iterator

import pytest

import flipjump
from flipjump import assemble_run_according_to_cmd_line_args
from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.interpreter.io_devices.KeyboardIO import KeyboardIO, ScriptedKeyEventSource
from flipjump.interpreter.io_devices.ScreenIO import XTERM256, InMemoryScreen
from flipjump.interpreter.io_devices.pygame_window import PcIO
from flipjump.utils.classes import TerminationCause
from flipjump.utils.exceptions import IODeviceException
from tests.unit.unit_utils import assemble_to_path

# the screen stream of io_devices/README.md: init_screen 4x2 format 8; define_palette slot 0, entries 0-1;
# draw_raw 0,1,0,1 / 1,0,1,0; present. The program outputs it byte by byte.
SCREEN_STREAM = (
    bytes([0x01, 4, 0, 2, 0, 8])
    + bytes([0x02, 0, 0, 1, 10, 20, 30, 200, 100, 0])
    + bytes([0x10, 0, 1, 0, 1, 1, 0, 1, 0])
    + bytes([0x1F])
)
SCREEN_PROGRAM = 'stl.startup\n' + ''.join(f'stl.output_char {byte}\n' for byte in SCREEN_STREAM) + 'stl.loop\n'

# the hash of a format-8 frame: the values, one byte each, then all 256 entries of the presented palette
EXPECTED_FRAME_HASH = hashlib.sha256(
    bytes([0, 1, 0, 1, 1, 0, 1, 0])
    + bytes([10, 20, 30, 200, 100, 0])
    + b''.join(bytes(colour) for colour in XTERM256[2:])
).hexdigest()

# 3-bit colours, written BIT by bit (stl.output_bit): init 8x1 format 3, then draw_raw of 1..7, 0 - the
# README's "8 pixels of 3 bits take 3 bytes" - then present.
PACKED_PROGRAM = (
    'stl.startup\n'
    + ''.join(f'stl.output_char {byte}\n' for byte in [0x01, 8, 0, 1, 0, 3, 0x10])
    + ''.join(f'stl.output_bit {(value >> i) & 1}\n' for value in [1, 2, 3, 4, 5, 6, 7, 0] for i in range(3))
    + 'stl.output_char 0x1F\nstl.loop\n'
)

# a program whose stream is wrong: a stray zero byte where a command should start
BAD_SCREEN_PROGRAM = SCREEN_PROGRAM.replace('stl.output_char 31\n', 'stl.output_char 0\n')
assert BAD_SCREEN_PROGRAM != SCREEN_PROGRAM


# polls the keyboard 5 tics (one status HEX each: 0x0 idle, 0x8 key-up, 0x9 key-down):
# prints D/U + the keycode char per event, '.' when idle, and '!' at the end.
KEYBOARD_PROGRAM = """
stl.startup_and_init_all

rep(5, i) poll_once

stl.output '!'
stl.loop

def poll_once @ event, down, up, after_downup, no_event, end < status, keycode, KEY_DOWN {
    hex.input_hex status
    hex.if status, no_event, event
  event:
    hex.input keycode
    hex.cmp 1, status, KEY_DOWN, up, down, up
  down:
    stl.output 'D'
    ;after_downup
  up:
    stl.output 'U'
  after_downup:
    hex.print keycode
    ;end
  no_event:
    stl.output '.'
  end:
}

status:   hex.hex
keycode:  hex.vec 2
KEY_DOWN: hex.hex 9
"""


def test_fj_program_drives_the_screen(tmp_path: Path, engine: str) -> None:
    fjm_path = assemble_to_path(SCREEN_PROGRAM, tmp_path, use_stl=True)
    frames_dir = tmp_path / 'frames'
    screen = InMemoryScreen(frames_dir=frames_dir)

    statistics = fjm_run.run(fjm_path, io_device=screen, show_progress=False)

    assert statistics.termination_cause == TerminationCause.Looping
    assert screen.frame_count == 1
    assert screen.frame_hashes[0][1] == EXPECTED_FRAME_HASH
    assert (frames_dir / 'frame_000000.png').exists()
    assert screen.last_frame_rgb[0] == (10, 20, 30)
    assert screen.last_frame_rgb[1] == (200, 100, 0)
    assert screen.get_output() == b''  # the output ended between commands


def test_fj_program_packs_3_bit_colours(tmp_path: Path, engine: str) -> None:
    fjm_path = assemble_to_path(PACKED_PROGRAM, tmp_path, use_stl=True)
    screen = InMemoryScreen()
    statistics = fjm_run.run(fjm_path, io_device=screen, show_progress=False)
    assert statistics.termination_cause == TerminationCause.Looping
    assert screen.frame_count == 1
    assert screen.pixel_indices == [1, 2, 3, 4, 5, 6, 7, 0]


def test_control_a_bad_stream_stops_the_run(tmp_path: Path, engine: str) -> None:
    fjm_path = assemble_to_path(BAD_SCREEN_PROGRAM, tmp_path, use_stl=True)
    screen = InMemoryScreen()
    with pytest.raises(IODeviceException) as failure:  # every engine raises the device's own error
        fjm_run.run(fjm_path, io_device=screen, show_progress=False)
    assert str(failure.value) == (
        'ScreenIO: reserved (0x00) at output byte 25, frame 0: opcode 0x00 is reserved (a stray zero byte?)'
    )
    assert screen.frame_count == 0


def test_scripted_keyboard_replay_is_deterministic(tmp_path: Path, engine: str) -> None:
    fjm_path = assemble_to_path(KEYBOARD_PROGRAM, tmp_path, use_stl=True)

    for _ in range(2):  # the replay is deterministic - two runs, identical output
        source = ScriptedKeyEventSource.from_text('0, down, 72\n2, up, 75\n')
        keyboard = KeyboardIO(source)
        statistics = fjm_run.run(fjm_path, io_device=keyboard, show_progress=False)
        assert statistics.termination_cause == TerminationCause.Looping
        assert keyboard.get_output() == b'DH.UK..!'


def test_pc_device_combines_keyboard_and_screen(tmp_path: Path, engine: str) -> None:
    # the complete PcIO device built from explicit components: screen out + (empty) keyboard in
    fjm_path = assemble_to_path(SCREEN_PROGRAM, tmp_path, use_stl=True)
    screen = InMemoryScreen(frames_dir=tmp_path / 'frames')
    keyboard = KeyboardIO(ScriptedKeyEventSource.from_text(''))
    statistics = fjm_run.run(fjm_path, io_device=PcIO(screen, keyboard), show_progress=False)
    assert statistics.termination_cause == TerminationCause.Looping
    assert screen.frame_count == 1


def test_pc_headless_runs_a_program(tmp_path: Path) -> None:
    # the headless 'pc' (#4): scripted keys in + PNG frames out, built programmatically
    fjm_path = assemble_to_path(SCREEN_PROGRAM, tmp_path, use_stl=True)
    events_path = tmp_path / 'events.txt'
    events_path.write_text('0, down, 72\n')
    frames_dir = tmp_path / 'frames'

    statistics = fjm_run.run(fjm_path, io_device=PcIO.headless(events_path, frames_dir), show_progress=False)
    assert statistics.termination_cause == TerminationCause.Looping
    assert (frames_dir / 'frame_000000.png').exists()


# ---------------------------------------------------------------- the end of the run (IODevice.end_of_run)

# init_screen 4x2 format 8, then a draw_raw cut after 2 of its 8 pixels; then the program ends by itself
CUT_STREAM = bytes([0x01, 4, 0, 2, 0, 8, 0x10, 0x01, 0x02])
CUT_MESSAGE = (
    'ScreenIO: draw_raw (0x10) at output byte 6, frame 0, pixel (2, 0): '
    "the program's output ended inside draw_raw (0x10): 24 of its bits read"
)
CUT_ENDINGS = {
    'looping': 'stl.loop\n',
    'null_ip': ';0\n',
    'eof': 'bit.input_bit in_bit\nstl.loop\nin_bit:\nbit.bit\n',
}


def cut_program(ending: str) -> str:
    return 'stl.startup\n' + ''.join(f'stl.output_char {byte}\n' for byte in CUT_STREAM) + CUT_ENDINGS[ending]


@pytest.mark.parametrize('ending', sorted(CUT_ENDINGS))
def test_a_run_whose_output_ends_inside_a_command_raises(tmp_path: Path, engine: str, ending: str) -> None:
    fjm_path = assemble_to_path(cut_program(ending), tmp_path, use_stl=True)
    with pytest.raises(IODeviceException) as failure:
        fjm_run.run(fjm_path, io_device=InMemoryScreen(), show_progress=False)
    assert str(failure.value) == CUT_MESSAGE


def test_the_cut_program_raises_through_flipjump_run(tmp_path: Path) -> None:
    fjm_path = assemble_to_path(cut_program('looping'), tmp_path, use_stl=True)
    with pytest.raises(IODeviceException) as failure:
        flipjump.run(fjm_path, io_device=InMemoryScreen(), show_progress=False, print_termination=False)
    assert str(failure.value) == CUT_MESSAGE


@pytest.fixture
def dummy_window(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    pygame = pytest.importorskip('pygame')
    monkeypatch.setenv('SDL_VIDEODRIVER', 'dummy')
    yield
    pygame.quit()


def test_the_cut_program_raises_through_the_cli(tmp_path: Path, dummy_window: None) -> None:
    """`fj --run cut.fjm --io pc`: the window's PcIO forwards end_of_run to its screen"""
    fjm_path = assemble_to_path(cut_program('looping'), tmp_path, use_stl=True)
    with pytest.raises(IODeviceException) as failure:
        assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', '--io', 'pc', str(fjm_path)])
    assert str(failure.value) == CUT_MESSAGE


def test_control_a_complete_program_does_not_raise_through_the_cli(tmp_path: Path, dummy_window: None) -> None:
    fjm_path = assemble_to_path(SCREEN_PROGRAM, tmp_path, use_stl=True)
    assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', '--io', 'pc', str(fjm_path)])


class ClosingScreen(InMemoryScreen):
    def __init__(self) -> None:
        super().__init__()
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


@pytest.mark.parametrize('program, raises', [(SCREEN_PROGRAM, False), (cut_program('looping'), True)])
def test_the_cli_closes_the_device_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str, raises: bool
) -> None:
    device = ClosingScreen()
    monkeypatch.setattr(flipjump.flipjump_cli, 'make_io_device', lambda mode: device)
    fjm_path = assemble_to_path(program, tmp_path, use_stl=True)
    with pytest.raises(IODeviceException) if raises else contextlib.nullcontext():
        assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', str(fjm_path)])
    assert device.closes == 1


def test_control_a_complete_program_does_not_raise(tmp_path: Path, engine: str) -> None:
    fjm_path = assemble_to_path(SCREEN_PROGRAM, tmp_path, use_stl=True)
    screen = InMemoryScreen()
    flipjump.run(fjm_path, io_device=screen, show_progress=False, print_termination=False)
    assert screen.frame_count == 1


class EndCountingIO(FixedIO):
    def __init__(self, fixed_input: bytes) -> None:
        super().__init__(fixed_input)
        self.ends = 0

    def end_of_run(self) -> None:
        self.ends += 1


@pytest.mark.parametrize(
    'source, cause',
    [
        ('stl.startup\nstl.loop\n', TerminationCause.Looping),
        ('stl.startup\n;0\n', TerminationCause.NullIP),
        ('stl.startup\nbit.input_bit b\nstl.loop\nb:\nbit.bit\n', TerminationCause.EOF),
    ],
)
@pytest.mark.parametrize('profile', [False, True])
def test_end_of_run_is_called_exactly_once(
    tmp_path: Path, engine: str, source: str, cause: TerminationCause, profile: bool
) -> None:
    fjm_path = assemble_to_path(source, tmp_path, use_stl=True)
    device = EndCountingIO(b'')
    statistics = fjm_run.run(fjm_path, io_device=device, show_progress=False, profile=profile)
    assert statistics.termination_cause == cause
    assert device.ends == 1


def test_end_of_run_is_not_called_after_a_memory_error(tmp_path: Path, engine: str) -> None:
    fjm_path = assemble_to_path('stl.startup\n;0x4000 << 40\n', tmp_path, use_stl=True)
    device = EndCountingIO(b'')
    statistics = fjm_run.run(fjm_path, io_device=device, show_progress=False)
    assert statistics.termination_cause == TerminationCause.RuntimeMemoryError
    assert device.ends == 0
