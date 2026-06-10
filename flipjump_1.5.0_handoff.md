# FlipJump 1.5.0 — Handoff

Scope of the **`flipjump` library** release that the DOOM-on-FlipJump project depends on. This is the
**upstream/toolchain half** only — the interpreter, devices, CLI, and STL additions that ship *in the
`flipjump` package as `fj==1.5.0`*. The game itself (renderer, sim, WAD pipeline, R0–R3) is a downstream
consumer and is **not** part of this release; it lives in the full plan (`doom_on_flipjump_plan.md`).

- **Baseline:** `fj==1.4.0` (released; merged into the working branch).
- **Target:** `fj==1.5.0`.
- **Authoring:** every `.fj` macro written through the **flipjump-dev skill** (`tomhea/skills`); in sessions
  without the plugin, clone the repo and read `plugins/flipjump/skills/flipjump-dev/SKILL.md` + `reference/`.
- **Universal correctness gate:** the **1,029-program catalog** (`pytest --catalog`, ~1,600 tests) must stay
  green after *every* change here — it is the strongest available test of self-modifying code, pointers, and
  IO, far beyond anything hand-written.
- **Universal methodology:** TDD — write the test first; assemble with `--werror`; verify byte-exact with
  `flipjump.assemble_and_run_test_output(...)` (not shell pipes); cover every behavior path with its own
  input (a single green fixture proved insufficient three times in the 1.4.0 catalog campaign); compare any
  value that can go negative with `hex.scmp`, never `hex.cmp`.

---

## WI-A · Interpreter speedup *(gating — everything downstream assumes this lands)*

Today `fj --run` ≈ **2M fj/s**; target **≥10M fj/s** (the DOOM budget is 1M fj-ops/frame at 10fps). Steps:

0. **Record the 1.4.0 baseline first.** `prime_sieve.fj` fj/s on the target hardware, at **w=32 and w=64**,
   so every later change has a denominator. Keep this as a tracked benchmark.
1. **Segment-aware memory.** A single flat `bytearray` can't model FlipJump's sparse segments. Use
   per-segment dense buffers. (Downstream keeps its hand-written address layout compact so dense storage
   stays cheap.)
2. **Fast-run mode.** A run path that turns off the optional, slow features:
   - per-op **statistics** (`op_counter` / flip / jump / `register_op`),
   - **trace**,
   - **breakpoints** / debug-ops list,
   - and **hoists the per-op IO-range check** out of the hot loop.
   A separate **profiling mode** keeps stats on (downstream needs per-region op counts); fast-run strips them.
3. **Decoded-word cache.** Cache decoded ops, **but FlipJump self-modifies**: `wflip` rewrites the jump words
   of *future* ops **anywhere in code space** — *not* merely "near `ip`". Invalidation must therefore be
   **address-keyed: any flip that lands in a cached word invalidates that entry** (tag/range dirty-check per
   flip, or page-granular invalidation — design is OQ-A1). ⚠️ The dirty-check sits **on the hot path**; its
   overhead must be **measured**, not assumed — it directly eats into the speedup.

**Benchmark:** `prime_sieve.fj`, w=32 vs w=64. **Acceptance:**
- ≥10M fj/s on `prime_sieve.fj` (else trigger the fallback), reported against the recorded baseline;
- **full catalog green** under fast-run mode + cache;
- correctness otherwise unchanged; a DOOM-scale **memory-footprint** test passes;
- a written w=32-vs-w=64 recommendation (speed + memory + pointer/`@` cost).

**Fallback if CPython plateaus:** move the interpreter loop to a **C-extension / Cython** or **PyPy**,
keeping the devices and debugger intact. (Schedule + complexity risk — see risks.)

**Open questions:** OQ-A1 cache-invalidation mechanism + its hot-path cost; OQ-A2 final w=32 vs w=64 call.

---

## WI-B · CLI device-IO + devices

The interpreter gains a device interface richer than the per-bit `read_bit`/`write_bit`, plus two devices.

1. **`--di` / `--do` CLI flags** selecting input/output devices:
   `fj --di keyboard --do InMemoryScreen256 doom.fjm`.
2. **Device↔memory hook (G17).** A new interpreter API letting a device **read interpreter memory by
   address** (the screen reads `SCREEN[]` and the palette table) and **write interpreter memory** (the
   keyboard writes the mailbox). This is the load-bearing new primitive — design its interface explicitly
   (OQ-B1).
3. **`InMemoryScreen256` (output device) — headless-first.** The FJ program emits a small **enum** selecting
   a function, then argument(s) as raw output bits; the device reads the referenced buffer from memory.
   - `init_screen(w, h, bpp, palette_size)` — first call; sizes the window, the packed framebuffer stride
     (**4bpp or 8bpp**), and the palette table (all program-driven, not hard-coded at construction).
   - `set_palette(palette_fj_address)` — read `palette_size`×RGB from that address.
   - `update_screen(screen_fj_address)` — read `W*H` packed indices, expand via palette, upscale, present.
   - `update_rectangle(w, h, rect_fj_address)` — partial update.
   - **Primary backend = headless:** writes frames to disk (PNG per frame / animated GIF / video via
     ffmpeg) **plus a per-frame hash log** for golden tests, and **timestamps presents** (measured fps comes
     from the device log, not hand-timing). An interactive window (pygame, optional `flipjump[screen]`
     extra) is a second backend behind the same interface.
   - Per-frame FJ cost ≈ one enum + one address (~a few `@`) ⇒ blit is ~free.
4. **Keyboard (input device) — non-blocking, virtual time, scriptable.** Host writes key events into a fixed
   **mailbox** region the FJ program polls once/tic (stream fallback: `0x00`=NO_EVENT, else
   `[0xFE][down/up][keycode]`; idle never EOFs). Event source is pluggable: **live keys** (interactive) **or
   a scripted event file** (`tic_number, down/up, keycode` lines) — the scripted source makes E2E tests and
   CI deterministic (our DOOM-demo-playback equivalent). **No timer device** — the FJ program keeps its own
   frame-counter clock.

**Acceptance (each TDD'd):** golden frame-hash tests for the screen device; scripted-input replay tests for
the keyboard; the memory hook unit-tested for read and write; the `--di/--do` plumbing tested end-to-end on a
tiny program. **Open questions:** OQ-B1 exact memory-hook API shape.

---

## WI-C · CLI debugger *(parallel, non-gating)*

Convert `fj --run`'s debugger from GUI to **CLI** (headless / agent-usable). Valuable for debugging the
renderer later, but nothing in the game's R0–R1 blocks on it — **schedule alongside WI-A/WI-B, not in front.**
Acceptance: existing debug capabilities (step, breakpoints, memory/label inspection, the verbose macro-stack
trace `fj -d` prints) reachable from a terminal with no GUI dependency; covered by tests.

---

## WI-D · STL additions — fixed-point math (no FP)

DOOM is entirely **16.16 fixed-point integer** math, so the STL gains fixed-point helpers — **no floating
point enters the STL.** Built on the existing `hex.mul` / `hex.div`, plus a host-side **LUT generator**.

- **`FixedMul` / `FixedDiv`** (16.16, and an **8.8 / narrow** variant for where precision allows — ≈4×
  cheaper mul). Note the intermediate-width trap (U5): a 16.16 `FixedMul` needs a 64-bit product (two words
  at w=32) with hand-carried overflow — budget the extra ops and test the overflow path explicitly.
- **LUT generator (host-side Python, emits `.fj` data tables):** reciprocal / scale / `yslope`, trig
  (`finesine`/`finecosine`/`finetangent`), `viewangletox`/`xtoviewangle`, distance colormaps. These replace
  **every** runtime divide (a `hex.div` ≈ 68K ops is fatal in a hot loop) with an integer-indexed lookup.
  Design constraint (U6): LUTs must be indexable **without a runtime-amount shift** (e.g. integer-distance
  index), since variable shifts are pointer-class expensive.
- **Strength-reduction helpers** where useful: `×constant` → constant shifts+adds (`mul` cost scales with the
  operand's on-bits, so sparse constants are cheap).

These are **library macros + a generator**, usable independently of DOOM; each is TDD'd against a host-side
reference (exact-output fixtures) and registered like catalog/hexlib tests so it runs under existing infra.

**Note on what is *reused*, not built (already in 1.4.0):** `hex.scmp` (signed compare), `hex.min`/`hex.max`,
`hex.ptr_index` / `read_nth_*` / `write_nth_*` (runtime-indexed access — the game's *fallback* primitive,
not a 1.5.0 deliverable), decimal/string IO. Don't reinvent these.

---

## Dependency order

```
WI-A (interpreter ≥10M fj/s)  ──┐
                                ├─→ gate for the game (R0–R3, separate plan)
WI-B (devices + memory hook)  ──┘
WI-D (fixed-point STL)  ── needed by R0 (LUTs) / R1 (renderer); independent of A/B, can start anytime
WI-C (CLI debugger)     ── parallel, non-gating
```

WI-A and WI-B are the **go/no-go gate**: if WI-A can't reach ~10M fj/s even with the C-ext/PyPy fallback, the
1M-ops/frame budget collapses and even flat 96×64 DOOM is out of reach (at 2M fj/s the budget is 200K
ops/frame). That verdict is reached *here*, in 1.5.0, before any game code is written.

---

## Risks owned by 1.5.0

- **R-A (highest) — 10M fj/s in CPython is unproven.** 2M→10M is 5×; the cache + fast-mode may not get there
  in pure Python, and the cache's address-keyed invalidation check is itself hot-path overhead. Mitigation:
  C-extension / Cython / PyPy fallback (schedule + complexity cost). *This is the project's primary gate.*
- **R-B — decoded-word cache correctness under self-modifying `wflip`.** The invalidation criterion is
  correctness-critical; the whole catalog staying green is the gate, not a hand-picked test.
- **R-C — assembler scalability at DOOM scale (informational for 1.5.0).** Mega-tables (colormaps, textures,
  font, map) may make the *assembler* slow/memory-hungry. 1.5.0 doesn't build those, but the LUT generator
  (WI-D) and zero-fill-segment handling should be validated to not regress assemble time.
- **R-D — w-width decision.** w=32 is favored (DOOM's 32-bit fixed-point; halves O(w) cost; skill + catalog
  data agree), but the final call is a WI-A benchmark output, and some intermediates need 64-bit width (U5).

---

## Acceptance for the 1.5.0 release as a whole

1. WI-A: `prime_sieve.fj` ≥10M fj/s (or fallback engaged), catalog green, w-recommendation written.
2. WI-B: both devices + memory hook TDD'd; `--di/--do` works; headless frame output + scripted input
   deterministic in CI.
3. WI-D: fixed-point macros + LUT generator, each tested byte-exact against a host reference.
4. WI-C: CLI debugger usable headless (may trail 1–3 without blocking the game).
5. **Whole catalog green throughout; no regressions in assemble time or memory.**
