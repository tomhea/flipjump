# FlipJump I/O devices

A FlipJump program has two I/O channels, an **input bit stream** and an **output bit stream**; every device
here is a protocol on top of them. The program writes commands to its output and reads events from its input.

| Device | File | Channels | Use |
|---|---|---|---|
| Standard | `StandardIO.py` | stdin / stdout | the default (`fj --io standard`) |
| PC | `pygame_window.py` | screen (output) + keyboard (input), one window | `fj --io pc` |
| Screen | `ScreenIO.py` | output | the screen protocol (headless or in the PC window) |
| Keyboard | `KeyboardIO.py` | input | the keyboard protocol (live or scripted) |
| Fixed | `FixedIO.py` | a bytes buffer in, captured out | tests |
| Broken | `BrokenIO.py` | none (raises on any I/O) | programs that must do no I/O |

The PC window needs `pip install flipjump[io]` (pygame-ce). A `.fjm` can carry its I/O mode in its header,
so `fj --run game.fjm` needs no `--io` flag.

---

## Keyboard

The program **polls**: it reads one status nibble per poll (`hex.input_hex`). There is no timer; the program
keeps its own frame clock, and idle polling never hits end-of-input.

| Status nibble | Meaning | Then |
|---|---|---|
| `0x0` | no event | — |
| `0x9` | a key was **pressed** | read the keycode byte (`hex.input`) |
| `0x8` | a key was **released** | read the keycode byte (`hex.input`) |

The keycode byte **must** be read after an event, or every later poll is out of phase.

### Keycodes

The codes are Arduino `Keyboard.h`'s: **ASCII below `0x80`**, the **modifiers at `0x80`–`0x87`**, and **every
other key at `0x88` + its USB HID usage ID**.

- A key is reported as the key, not as what it types: letters are lowercase (`'a'` = 97) and the digit row is
  `'0'`–`'9'`, whatever modifiers are held. Shift is its own event.
- A key with an ASCII code is always sent as it (Backspace `8`, Tab `9`, Enter `13`, Esc `27`, Space `32`), so
  `0x88`–`0xC0` are never sent. Delete is `0xD4` (its HID code); ASCII 127 is never sent.
- The printable codes follow the keyboard layout's unshifted key (on a French layout the key that types `&`
  sends `'&'`). A key whose unshifted character is not ASCII is not delivered.

| Code | Key |
|---|---|
| `0x08` `0x09` `0x0D` `0x1B` | Backspace, Tab, Enter, Esc |
| `0x20`–`0x7E` | the printable keys: Space, `'0'`–`'9'`, `'a'`–`'z'`, punctuation (unshifted) |
| `0x80` `0x81` `0x82` `0x83` | Left Ctrl, Left Shift, Left Alt, Left GUI (Windows / Command) |
| `0x84` `0x85` `0x86` `0x87` | Right Ctrl, Right Shift, Right Alt, Right GUI |
| `0xC1` | Caps Lock |
| `0xC2`–`0xCD` | F1–F12 |
| `0xCE` `0xCF` `0xD0` | Print Screen, Scroll Lock, Pause |
| `0xD1` `0xD2` `0xD3` | Insert, Home, Page Up |
| `0xD4` `0xD5` `0xD6` | Delete, End, Page Down |
| `0xD7` `0xD8` `0xD9` `0xDA` | **Right, Left, Down, Up** |
| `0xDB` | Num Lock |
| `0xDC` `0xDD` `0xDE` `0xDF` `0xE0` | keypad `/` `*` `-` `+` Enter |
| `0xE1`–`0xE9`, `0xEA`, `0xEB` | keypad `1`–`9`, `0`, `.` |
| `0xED` | Menu (the Application key, HID `0x65`) |
| `0xF0`–`0xFB` | F13–F24 |

A program that wants "either Ctrl" tests both `0x80` and `0x84` (the same for Shift, Alt and GUI).
Keys not in the table are not delivered; in the PC window F11 toggles fullscreen and is never delivered.
Scripted input (tests, replays) is a text file of `tic, down/up, keycode` lines (`ScriptedKeyEventSource`).

---

## Screen

### How a command is written

1. A command starts on a **byte boundary** with its **opcode byte**.
2. Its fields follow **packed back to back at exactly their width in bits**, least-significant bit first
   (the order FlipJump outputs bits in): 8 pixels of 3 bits take 3 bytes.
3. The command ends with **zero bits up to the next byte boundary**.
4. Multi-byte numbers (`w`, `h`, ...) are little-endian.
5. Nothing reaches the screen until **`present`** (`0x1F`), except the black screen `init_screen` shows.

The screen **never reads program memory**: every command carries its data in the output stream.

### Opcodes

| Opcode | Command | Fields |
|---|---|---|
| `0x00` | — | reserved: an error (catches a stray zero byte) |
| `0x01` | `init_screen` | `w:16` `h:16` `format:8` |
| `0x02` | `define_palette` | `slot:8` `first:B` `last:B` then `(last−first+1) × (R:8 G:8 B:8)` |
| `0x03` | `set_max_fps` | `fps:8` (0 = no cap) |
| `0x04` | `set_window_title` | UTF-8 bytes, then `0x00` |
| `0x05` | `set_window_icon` | `w:8` `h:8` then `w×h` colours of `B` bits |
| `0x06` | `select_palette` | `slot:8` |
| `0x07` | `builtin_palette` | `slot:8` `id:8` |
| `0x10` | `draw_raw` | `w×h` colours of `B` bits, row-major |
| `0x11` | `draw_collines` | columns (below), ended by the top column value |
| `0x1F` | `present` | — |

`B` = the format's bits per colour. `init_screen` must come first; `0x02`, `0x06`, `0x07` exist only in indexed
formats.

### Formats (`init_screen`'s `format` = bits per colour)

| `format` | Model | Default palette |
|---|---|---|
| `1` | indexed | black, white |
| `2` | indexed | 4 greys |
| `4` | indexed | the 16 EGA/VGA colours |
| `8` | indexed | xterm-256: the 16 colours, a 6×6×6 RGB cube, 24 greys |
| `3`, `5`–`7`, `9`–`16` | indexed | the RGB cube of that many bits |
| `24` | direct RGB, `R:8 G:8 B:8` (`R` the first, lowest byte) | none (no palettes) |

Any other `format` is refused. **The RGB cube of `b` bits**: green gets `⌈b/3⌉` bits, red `⌈(b − green)/2⌉`,
blue the rest (3 → 1:1:1, 12 → 4:4:4, 16 → 5:6:5); the value is red, green, blue from the high bits down (at 16,
RGB565), and each channel is scaled to 0-255 by `round(level × 255 / (2^bits − 1))`.

### Palettes (indexed formats)

- The picture holds **colour indices**; the palette **selected when `present` runs** gives them their RGB, so a
  flash or a fade costs 2 bytes, not a redraw.
- 256 slots, each starting as the format's default. `define_palette` sets entries `first`…`last` (inclusive) of
  one slot, always as 8-bit R, G, B; a large palette can be sent in several ranges.
- `builtin_palette` copies a built-in into a slot. A built-in has exactly `2^B` entries: a longer one is cut, a
  shorter one padded with black.

| `id` | Built-in |
|---|---|
| `0` | black / white |
| `1` | grey ramp, `2^B` levels |
| `2` | EGA-16 |
| `3` | xterm-256 |
| `4` | the RGB cube of `B` bits |

The icon's colours go through the palette selected when `set_window_icon` runs (format 24: their RGB); the icon
has no transparency.

### The screen's life cycle

| Moment | What happens |
|---|---|
| `init_screen` | The window shows a **black** `w×h` screen at once. The picture is colour 0 everywhere, every palette slot holds the format's default, and slot 0 is selected. |
| before the first `present` | The window stays black; the headless backend records no frame. |
| `present` | Shows the picture through the selected palette, and counts as a frame — also with nothing drawn since the last one (the `set_max_fps` wait applies, and the headless backend records it). |
| another `init_screen` | Starts over like the first, at the new `w×h`: the format, field widths, palettes, selected slot and picture all reset. The title, icon and max fps stay; the frame count keeps counting. |

### `draw_collines` — the picture as column runs

Each column is a list of **runs** from the top down; the program has already resolved what is in front, so the
device only fills. **Field widths** (fixed by `init_screen`, a multiple of 4 bits):

| Field | Width | Top values are tokens | Limit |
|---|---|---|---|
| row (`y2`, `y`) | `R` = the smallest of 4, 8, 12, 16 with `h < 2^R − 4` | the top **4** | `h ≤ 11 / 251 / 4,091 / 65,531` |
| column (`x`) | `C` = the smallest of 4, 8, 12, 16 with `w < 2^C − 1` | the top **2** | `w ≤ 14 / 254 / 4,094 / 65,534` |

```
draw_collines := 0x11  ( column )*  END_CMD
column        := x  item*  END                  x = the column to draw (any order; skips, repeats allowed);
                                                END closes it (rows below the cursor are left as they are)
item          := y2 colour                      fill rows [cursor, y2) with colour; cursor = y2
               | KEEP y                         rows [cursor, y) keep what the screen shows there; cursor = y
               | COPY y                         rows [cursor, y) copy the column to the left (x − 1); cursor = y
```

| Row-field value | Token | At `R = 8` |
|---|---|---|
| `2^R − 1` | END (column) | `0xFF` |
| `2^R − 2` | KEEP | `0xFE` |
| `2^R − 3` | COPY | `0xFD` |
| `2^R − 4` | reserved | `0xFC` |
| `0 … h` | a row | `0x00`–`0xFB` |

| Column-field value | Token | At `C = 8` |
|---|---|---|
| `2^C − 1` | END_CMD: the command is over | `0xFF` |
| `2^C − 2` | reserved | `0xFE` |
| `0 … w−1` | a column | `0x00`–`0xFD` |

- Each column's cursor starts at row 0. A fill, KEEP or COPY moves it to `y2` / `y` with `cursor ≤ y2 ≤ h`; an
  item of zero rows paints nothing, and only a row behind the cursor is an error.
- END always closes a column, also once the cursor reached `h` (after which only END or zero-row items may follow).
- **KEEP** keeps rows `[cursor, y)` as they are: the last presented frame's pixels, or this frame's if it already
  drew them. **COPY** copies the same rows of column `x − 1` as it is at that moment (likewise new or last frame's
  pixels); COPY in column 0 is an error, whatever its `y`.

### Example

A 4×3 screen, format 8 (`R = C = 4`; the first field of each byte is its low nibble): column 0 filled
with colour 5, column 1 copied from it, then shown.

```
01  04 00  03 00  08      init_screen  w=4 h=3 format=8
11                        draw_collines
30 05                       x=0, y2=3 | colour 0x05            (column 0: rows 0-2 = 5)
1F                          END | x=1                         (column 0 closed; column 1 ...)
3D                          COPY | y=3                        (... rows 0-2 copied from column 0)
FF                          END | END_CMD                     (column 1 closed; the command ends on a byte boundary)
1F                        present
```

### Errors

Every error raises `IODeviceException`, saying **where** and **why, with the numbers**:

```
ScreenIO: draw_collines (0x11) at output byte 1,204, frame 37, column 17, cursor 40: y2 = 90 is past the screen (rows end at h = 84)
```

- **Where**: the command and its opcode (`unknown (0xNN)` / `reserved (0x00)` for a byte that is not one); the byte
  offset of its opcode in the program's output (from 0); the frame (the `present`s so far); inside a draw, the
  column `x` and the cursor (the pixel `(x, y)` in `draw_raw` and the icon).
- **Why**: the field, the value it got, and the values it allows:

| Error | The reason, for example |
|---|---|
| unknown opcode | `opcode 0x23 is not a command (commands: 0x01-0x07, 0x10, 0x11, 0x1F)` |
| reserved opcode | `opcode 0x00 is reserved (a stray zero byte?)` |
| a command before `init_screen` | `select_palette (0x06) before init_screen: the first command must be init_screen (0x01)` |
| screen size | `w = 0: must be 1..65,534` · `h = 65,534: must be 1..65,531` |
| format | `format = 20: must be 1-16 or 24` |
| a palette command in RGB | `define_palette (0x02) in format 24 (direct RGB): palettes exist only in formats 1-16` |
| palette range | `first = 12 > last = 3` |
| built-in palette | `id = 7: the built-ins are 0..4` |
| column | `x = 170: the screen has columns 0..159 (w = 160)` |
| reserved column token | `x = 0xFE is the reserved column token (2^C - 2, C = 8)` |
| row past the screen | `y2 = 90 is past the screen (rows end at h = 84)` |
| a row behind the cursor | `y2 = 30 is behind the cursor (need 40 <= y2 <= 84)` |
| reserved row token | `0xFC is the reserved row token (2^R - 4, R = 8)` |
| COPY in column 0 | `COPY copies column x - 1, and column 0 has none` |
| non-zero padding | `the padding bits after the command are 0b0101, not zero (the stream is probably misaligned)` |
| icon size | `set_window_icon: w = 0: must be 1..255` |
| title | `set_window_title: title byte 5 (0xC3) is not valid UTF-8` |
| output ends inside a command | `the program's output ended inside draw_collines (0x11): 37 of its bits read` |
| output ends inside an opcode byte | `the program's output ended inside an opcode byte: 3 of its 8 bits read` (where: `an opcode at output byte N, frame F`) |

"N of its bits read" counts from the command's opcode. It is checked when the run ends by itself (looping, a null
jump, end of input).

### Backends

The headless backend (`InMemoryScreen`, the default in tests) keeps, per `present`, the RGB frame
(`last_frame_rgb`), a PNG when given a `frames_dir`, and a `(time ns, sha256)` in `frame_hashes` (for golden tests);
it never waits. The sha256 is of the picture's values, row-major (one byte each in formats 1-8, two little-endian
in 9-16), then the RGB bytes of all `2^B` entries of the presented palette; in format 24, of its R, G, B bytes.
The PC window shows a scaled frame, and `set_max_fps` makes `present` wait.

A subclass may claim an opcode that is not a command through `InMemoryScreen._extension_command` (not part of
the wire API; a test harness multiplexes its own messages this way).
