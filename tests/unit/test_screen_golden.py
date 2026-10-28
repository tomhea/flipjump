"""
a frozen golden of the screen and keyboard devices: every command stream, key script and scripted-keyboard text
the device tests fed them (recorded at 2e78aa9 into screen_golden_corpus.json.gz, by logging each device's
write_bit / write_byte / end_of_run / get_output and read_bit calls), replayed and digested to the byte:
every call's result and error text, each frame's hash, the PNG files, the final picture, palettes, title, icon and
max fps; the keyboard's bit sequence; the window's surface after each present, its caption, and the keys it delivers.

a restructuring of the devices must keep every digest. The negative controls move a real device constant and
require a digest to change.
"""

import gzip
from array import array
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from flipjump.interpreter.io_devices import ScreenIO
from flipjump.interpreter.io_devices.KeyboardIO import KeyboardIO, KeyEvent, ScriptedKeyEventSource
from flipjump.interpreter.io_devices.pygame_window import KEYCODES
from flipjump.interpreter.io_devices.ScreenIO import InMemoryScreen
from flipjump.utils.exceptions import IODeviceException

CORPUS_PATH = Path(__file__).with_name('screen_golden_corpus.json.gz')

# sha256 of each section's per-entry digests, joined (the corpus file holds the per-entry ones)
SECTION_DIGESTS = {
    'screens': 'c643658224d2153bad483902687fa1792dc7e44c1b4d9978a9e97fd0000e0a25',
    'keyboards': 'ddb4ae4de902f161593f51debea15f35488d2c2ea13377c17d8be71810e52578',
    'texts': '378402ea715f22488f100f243644ff61c4517879581b887cfed0e6f8119e6919',
    'keycodes': '386dfe21ea9998d16021b080953f97ae483406c8b86e0552ca9d03fd08110ad1',
    'interactive': 'cb26fdff1b94befa0479e950e5df439c5a29f6d2b31fd5134ea1ecaac8707932',
    'window_keys': 'a84da427b2f0135b873a60cd2c95c101eb4111c14b93d4c20d781dfa524d13d4',
}

REAL_RGB_CUBE_SPLIT = ScreenIO.rgb_cube_split
Notes = List[Any]


def load_corpus() -> Dict[str, Any]:
    with gzip.open(CORPUS_PATH, 'rt', encoding='ascii') as corpus_file:
        return json.load(corpus_file)  # type: ignore[no-any-return]


def _sha(data: Any) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else repr(data).encode('utf-8')).hexdigest()


def replay_screen(log: Dict[str, Any], device: Any, after_call: Optional[Callable[[Notes], None]] = None) -> Notes:
    """feed the logged calls to `device` one by one, noting every result and error, then the device's state"""
    notes: Notes = []
    calls: List[Any] = []
    for op in log['ops']:
        if op[0] == 'B':
            calls += [('write_byte', (byte,), {}) for byte in bytes.fromhex(op[1])]
        elif op[0] == 'T':
            calls += [('write_bit', (bit == '1',), {}) for bit in op[1]]
        elif op[0] == 'E':
            calls.append(('end_of_run', (), {}))
        else:
            calls.append(('get_output', (), {'allow_incomplete_output': op[1]}))
    for index, (method, args, kwargs) in enumerate(calls):
        try:
            result = getattr(device, method)(*args, **kwargs)
            if result is not None:
                notes.append((index, method, result))
        except Exception as error:  # every error is part of the behaviour
            notes.append((index, method, type(error).__name__, str(error)))
        if after_call is not None:
            after_call(notes)
    notes.append(
        (
            device.width,
            device.height,
            device.bpp,
            device.frame_count,
            device.selected_slot,
            device.max_fps,
            device.window_title,
            device.window_icon,
            [frame_hash for _, frame_hash in device.frame_hashes],
            _sha(array('L', device.pixel_indices).tobytes()),
            _palettes_sha(device.palettes),
            _palettes_sha([device.palette]),
            _sha(device.last_frame_rgb),
        )
    )
    return notes


def _palettes_sha(palettes: List[Any]) -> str:
    """the slots' contents in order; a list shared by many slots (format 16: 65,536 entries each) is hashed once"""
    by_id: Dict[int, str] = {}
    for slot in palettes:
        if id(slot) not in by_id:
            by_id[id(slot)] = _sha(slot)
    return _sha([by_id[id(slot)] for slot in palettes])


def _png_sha(png: bytes) -> str:
    """a PNG's chunks with its IDAT decompressed: zlib's compressed bytes differ between platforms' zlib builds"""
    chunks: List[Any] = []
    at, idat = 8, b''
    while at < len(png):
        length, kind = struct.unpack_from('>I4s', png, at)
        data = png[at + 8 : at + 8 + length]  # noqa: E203
        if kind == b'IDAT':
            idat += data
        else:
            chunks.append((kind, data))
        at += 12 + length
    return _sha((png[:8], chunks, zlib.decompress(idat)))


def screen_digest(log: Dict[str, Any], frames_dir: Path) -> str:
    notes = replay_screen(log, InMemoryScreen(frames_dir=frames_dir))
    pngs = sorted(frames_dir.glob('*.png')) if frames_dir.exists() else []
    notes.append([(png.name, _png_sha(png.read_bytes())) for png in pngs])
    return _sha(notes)


def keyboard_digest(log: Dict[str, Any]) -> str:
    device = KeyboardIO(ScriptedKeyEventSource([KeyEvent(*event) for event in log['events']]))
    bits = ''.join('1' if device.read_bit() else '0' for _ in range(log['reads']))
    return _sha((bits, device.tic))


def text_digest(text: str) -> str:
    try:
        source = ScriptedKeyEventSource.from_text(text)
    except IODeviceException as error:
        return _sha('error: ' + str(error))
    events = list(source.events)
    device = KeyboardIO(source)
    reads = 12 * (len(events) + max([event.tic for event in events], default=0) + 3)
    return _sha((events, ''.join('1' if device.read_bit() else '0' for _ in range(reads)), device.tic))


def device_digests(corpus: Dict[str, Any], tmp_path: Path) -> Dict[str, List[str]]:
    return {
        'screens': [screen_digest(log, tmp_path / f'frames_{index}') for index, log in enumerate(corpus['screens'])],
        'keyboards': [keyboard_digest(log) for log in corpus['keyboards']],
        'texts': [text_digest(text) for text in corpus['texts']],
        'keycodes': [_sha(sorted(KEYCODES.items()))],
    }


def window_digests(corpus: Dict[str, Any]) -> Dict[str, List[str]]:
    """the InteractiveScreen streams through a real (SDL dummy) window, and the keys the window delivers"""
    import pygame

    from flipjump.interpreter.io_devices.pygame_window import InteractiveScreen, PygameWindow

    digests = []
    window = PygameWindow()  # one window for all: SDL takes ~2 s to make a SCALED window's renderer
    for log in corpus['screens']:
        if log['cls'] != 'InteractiveScreen':
            continue
        device = InteractiveScreen(window=window)
        frames = [0]

        def after_call(notes: Notes) -> None:
            if device.frame_count != frames[0]:
                frames[0] = device.frame_count
                surface = pygame.display.get_surface()
                assert surface is not None
                notes.append((surface.get_size(), _sha(pygame.image.tobytes(surface, 'RGB'))))
                notes.append(pygame.display.get_caption())

        digests.append(_sha(replay_screen(log, device, after_call)))

    window.ensure_open_for_input()
    delivered = []
    for key in [*range(0x200), *[(1 << 30) | usage for usage in range(0x100)], pygame.K_F11]:
        for event_type in (pygame.KEYDOWN, pygame.KEYUP):
            pygame.event.post(pygame.event.Event(event_type, key=key))
        window.pump_events()
        delivered.append((key, list(window.key_events)))
        window.key_events.clear()
    window.close()
    return {'interactive': digests, 'window_keys': [_sha(delivered)]}


@pytest.fixture
def headless_sdl(monkeypatch: pytest.MonkeyPatch) -> Any:
    pygame = pytest.importorskip('pygame')
    monkeypatch.setenv('SDL_VIDEODRIVER', 'dummy')
    monkeypatch.setenv('SDL_AUDIODRIVER', 'dummy')
    yield
    pygame.quit()


def _check(section: str, got: List[str], expected: List[str]) -> None:
    changed = [index for index, (a, b) in enumerate(zip(got, expected)) if a != b]
    assert len(got) == len(expected) and not changed, f'{section}: entries {changed} changed'
    assert _sha(got) == SECTION_DIGESTS[section], section


def test_the_devices_match_the_golden(tmp_path: Path) -> None:
    corpus = load_corpus()
    assert len(corpus['screens']) > 100 and len(corpus['keyboards']) > 5 and len(corpus['texts']) > 5
    for section, digests in device_digests(corpus, tmp_path).items():
        _check(section, digests, corpus['digests'][section])


def test_the_window_matches_the_golden(headless_sdl: None) -> None:
    corpus = load_corpus()
    for section, digests in window_digests(corpus).items():
        _check(section, digests, corpus['digests'][section])


@pytest.mark.parametrize(
    'name, value',
    [
        ('EGA16', ((0, 0, 1),) + ScreenIO.EGA16[1:]),  # a palette entry
        ('rgb_cube_split', lambda bits: REAL_RGB_CUBE_SPLIT(bits)[::-1]),  # a format's bit split
    ],
)
def test_negative_control_a_moved_device_constant_changes_a_digest(name: str, value: Any, tmp_path: Path) -> None:
    corpus = load_corpus()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(ScreenIO, name, value)
        ScreenIO.builtin_palette.cache_clear()
        try:
            mutated = [screen_digest(log, tmp_path / f'm{index}') for index, log in enumerate(corpus['screens'])]
        finally:
            ScreenIO.builtin_palette.cache_clear()
    changed = sum(a != b for a, b in zip(mutated, corpus['digests']['screens']))
    assert changed > 0, f'moving ScreenIO.{name} changed no digest: the golden does not see it'


if __name__ == '__main__':  # freeze: python -m tests.unit.test_screen_golden <tmp dir>  (on the frozen commit only)
    import os

    os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
    os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
    frozen = load_corpus()
    frozen['digests'] = {**device_digests(frozen, Path(sys.argv[1])), **window_digests(frozen)}
    with gzip.open(CORPUS_PATH, 'wt', encoding='ascii') as out:
        json.dump(frozen, out, sort_keys=True)
    for frozen_section, frozen_list in frozen['digests'].items():
        print(f"    '{frozen_section}': '{_sha(frozen_list)}',  # {len(frozen_list)} entries")
