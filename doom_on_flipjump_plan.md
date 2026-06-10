# Running DOOM on FlipJump — Plan

*(Rebuilt against the released `fj==1.4.0` baseline + the live `flipjump-dev` skill; adds the
previously-missing game-simulation half, the map-gridification work item, headless-first devices,
and fixes the decoded-cache invalidation criterion.)*

## Context & key decisions

FlipJump is a one-instruction language ("flip a bit, then jump"). Goal: run DOOM on it, driven by our own
screen + keyboard devices, as fast as we can. Two facts dominate every decision:

1. **Pointer accesses cost ~37–55× a fixed-address access** (a runtime pointer must rebuild its `w`-bit
   address bit-by-bit into a flip-opcode; the `w(0.5@+2)` term ≈ ~32@ at w=64, vs ~@ for a compile-time-fixed
   address — `stl/hex/pointers/xor_to_pointer.fj:32`). So **everything heavy must live at compile-time-fixed
   addresses**, accessed `hex.hex`-style, never `hex.pointers`.
2. **The interpreter is the gate.** Today `fj --run` is ~2M fj/s; we target **10M fj/s** and budget **≤1M
   fj-ops/frame** (10M ÷ 10fps). All math respects that ceiling.

**Resolved:**
- **No c2fj.** The whole engine is **hand-written FlipJump on fixed addresses** — c2fj's pointer-based RISC-V
  memory can't fit the budget anyway. We use DOOM's *source as the algorithm reference* and reimplement its
  renderer by hand (raycaster-grade first, scaling toward DOOM fidelity).
- **No floating point.** DOOM is entirely **16.16 fixed-point integer** math — FlipJump needs *no* FP. We add
  fixed-point helpers (`FixedMul`/`FixedDiv`) built on `hex.mul`/`hex.div` + **reciprocal/trig LUTs**; no FP
  in the 1.5.0 stl.
- **Target `w=32`** (validate in MP-A): DOOM's fixed-point is 32-bit, so one word = one number; halving word
  width roughly halves address/pointer cost and the per-op data the interpreter moves — and the skill's own
  guidance confirms `-w 32` "halves every O(w) op for both compile and run". Fall back to w=64 only if
  memory-constrained.
- **No timer device** (see G4): time is a virtual clock = internal frame counter; the sim advances one tic
  per rendered frame. Deterministic, and no real-time input needed.

## Methodology: test-driven, every macro in isolation

**Non-negotiable:** unit-test **every** FlipJump macro the moment it's written, in a sterile environment,
using the repo's test framework + `FixedIO` (compare exact output for crafted inputs). Same for the new
1.5.0 interpreter changes and the Screen/Keyboard devices. Write the test first, then the macro. Bugs in
hand-written FlipJump are brutal to find later — TDD up front is the only sane path.

**Keep compile+run fast — it's a hard methodology constraint, not a nicety.** Lever 0 trades
runtime for assembled size, so a careless macro makes every build take minutes and the TDD loop collapses.
Rules: **factor heavy bodies into one `stl.fcall`/`stl.fret` leaf subroutine** (≈`@-1`/≈1, ~0 extra space)
instead of inlining them across a `rep`; keep the inlined per-iteration stub to *a few* ops; share geometry
per-column (unroll 96×, not 6144×); test macros on tiny inputs (a 4×4 fill, not 96×64) so unit builds stay
sub-second; and treat **assemble time + `.fjm` size as tracked metrics** alongside ops/frame, starting at R1.

**Authoring rules adopted from the 1.4.0 catalog campaign** (these bit for real — three latent catalog bugs
shipped past green fixtures before the Pass-4 code review caught them):
- **Any value that can go negative is compared with `hex.scmp`, never `hex.cmp`.** The renderer/sim are full
  of signed quantities (deltas, velocities, angle wraps, `mid-1` underflows) — the unsigned-compare trap is
  the #1 latent-bug class observed in practice. Make it a review checklist item on every `.fj` PR.
- **Check the STL before hand-rolling anything** (`min`/`max`/`abs`/`mov`/compare/IO all exist); assemble
  with `--werror`; verify byte-exact via `flipjump.assemble_and_run_test_output(...)` — the skill's
  verification loop, not ad-hoc shell pipes.
- **Cover every behavior path with its own input** — a single green fixture proved insufficient three times
  in the catalog; renderer macros get boundary inputs (0-height column, full-height, off-screen) by default.

## Versioning & tooling

- **Baseline `fj==1.4.0` — released and merged into this branch.** All work ships as **`fj==1.5.0`**.
- **The `flipjump-dev` skill is live** (`/plugin marketplace add tomhea/skills`,
  `/plugin install flipjump@tomhe`) and is the authoritative how-to-write-FlipJump reference (the old
  `flipjump_claude_conclusions.md` was consolidated into it and deleted). Every hand-written `.fj` file is
  authored through it. *Remote/cloud sessions where the plugin isn't installed:* clone
  `github.com/tomhea/skills` and read `plugins/flipjump/skills/flipjump-dev/SKILL.md` + `reference/`
  directly — verified to work from this environment.
- **The demonstration catalog is complete: 1,029 programs** (`programs/catalog/`, registered in test CSVs,
  `pytest --catalog` ≈ 1,600 tests). Two roles for us: (a) a **pattern library** for idioms; (b) a
  **regression gate for every MP-A interpreter change** — the fast-run mode and decoded-word cache must keep
  the whole catalog green, which is a far stronger correctness suite than anything we'd write ad hoc.
- **New 1.4.0 STL we lean on instead of inventing:** `hex.scmp` (signed compare), `hex.ptr_index` /
  `read_nth_*` / `write_nth_*` (runtime-indexed access — the *fallback* primitive if Lever-0 unrolling
  proves too costly, never the default), `hex.min/max`, the string/print helpers (debug output).

## Goals (tiers) & per-tier feature support

| Tier | Res | FPS | Realistic feature set |
|------|-----|-----|------------------------|
| **Minimal (primary)** | 96×64 | 10 | **3D view only**, flat-shaded walls/floors/ceilings; sim scope **S0–S1** (below). **No** HUD/status-bar, menu, on-screen text, or textures; sprites minimal/none. |
| Very good | 160×100 | 15 | textures on (if speed allows), basic sprites + **S2** enemies, maybe a reduced HUD; small but readable. |
| Best | 320×200 | 25 | full textures, sprites, HUD/text; needs substantially more fj/s. |

`RESX`/`RESY`, a `TEXTURED` flag (default `0`=flat), and **`BPP` (4 or 8)** are compile-time knobs. Flat
minimal likely needs ≤16 colors — at **4bpp, w=32 packs 8 px/word**, halving framebuffer writes vs 8bpp
(~768 word-writes/frame instead of ~1,536). At 96×64 the 320-px-wide HUD, menus, and text simply don't fit —
hence "3D view only" for the minimal tier. Features return as resolution and budget grow.

## What 1M ops/frame buys (the *corrected*, honest reckoning)

⚠️ **An earlier version of this plan under-counted the framebuffer write by ~6×.** The real stl costs
(measured, w=32, `@≈27` at DOOM scale) are the foundation, so state them plainly:

| Operation | stl macro | cost (w=32) | ≈ fj-ops |
|-----------|-----------|-------------|----------|
| Fixed-address byte write | `hex.xor`-style | ~7@ | ~190 |
| **Pointer** byte write (runtime addr) | `write_byte` | 41@+197 | **~1,300** |
| **Pointer** byte read (runtime addr) | `read_byte` | ~33@ | **~1,000** |
| **Indexed** read/write (`read_nth_*`) | `ptr_index`+… | O(w) ops **and** O(w) space *per call site* | ~pointer-class |
| 32-bit multiply (8 hex) | `hex.mul` | ~350@+1300 | **~10,000** |
| 16-bit multiply (8.8, 4 hex) | `hex.mul` | ~88@+320 | **~2,700** |
| 32-bit **divide** (8 hex) | `hex.div` | ~2300@+6400 | **~68,000** |
| constant shift (`<<`/`>>` by const) | `hex.shl_hex` | ~8@+32 | **~250** |

**The trap:** a framebuffer pixel at runtime `(x,y)` lives at a runtime-computed address, so each pixel write
is a **pointer write (~1,300 ops), not the ~190-op fixed write first assumed.** Naively: 6144 px × ~1,300 ≈
**~8M ops/frame just for writes** — ~8× over the *entire* 1M budget. InMemoryScreen256 makes the *blit* free
but does **nothing** for the cost of *constructing* the framebuffer in memory.

**The mitigation (see Lever 0): compile-time unrolling + packed words ⇒ fixed-address, pointer-free.**
- Unroll the framebuffer fill with `rep` so every address is a compile-time constant ⇒ **~7@ fixed write,
  not ~41@ pointer write.** Combine with **packed words** (8 px/word at 4bpp, 4 px/word at 8bpp, w=32):
  ~768–1,536 word-writes × ~7@ ≈ **~150–290K ops/frame** for a full flat 96×64, with **zero pointers**.
- This is *why* DOOM draws memory-coherent columns/spans — we go further and make the runs **compile-time
  constant**.

**Hard rule learned from the skill (measured in the catalog campaign): never `rep`-unroll an O(w)-sized
macro.** 243 unrolled indexed (`read_nth_*`) reads took **~7 minutes to assemble**; ~729 of them ran ~12s
interpreted. Only **O(1)-size bodies** (fixed-address ops, a few flips, an `fcall`) may be unrolled. This is
the quantitative backing for U0 and the design rule for every Lever-0 stub.

**Revised verdict:**
- **Flat 96×64:** plausible via Lever-0 unrolling + packed writes + near-zero runtime mul/div. The binding
  constraint flips from *ops/frame* to **compile time / assembled size** (keep `calc_pixel` tiny). **R1 must
  measure both ops/frame *and* compile time before we trust it.**
- **Textured 96×64:** adjacent pixels differ ⇒ can't pack, and texels are value-indexed reads (jump-table,
  not pointer, but still real work) ⇒ several K ops/px × 6144 ⇒ **multi-M/frame** *plus* a large texture
  switch-table inflating compile time. Treat textures as an R3 stretch, not a tier-2 promise.

**Per-column/per-span math is the *second* threat.** A 32-bit `FixedMul` ≈ ~10,000 ops and a `FixedDiv` ≈
**~68,000 ops**; even **one divide per column** × 96 ≈ **~6.5M ops — the budget many times over.** So:
- **Never divide at runtime** — replace *every* divide with an integer-indexed **reciprocal/scale/yslope
  LUT** (value-indexed jump, not pointer).
- **Cheaper multiplies:** (a) **strength-reduce `×constant` to constant shifts+adds** (~250 ops
  each, and `mul` cost scales with the *on-bits* of the operand, so sparse constants are cheap); (b) use
  **8.8 / narrow** widths (~2,700 ops) where precision allows; (c) **LUT-multiply** when one operand is
  bounded; (d) prefer **incremental adds (DDA)** over per-step muls. Aim for the per-column path to contain
  *no* full 32-bit mul/div at all.

## How DOOM rendering works (drives what we build)

Player state: position `(x,y)`, eye height `z`, view angle. Per frame:
1. **Visibility (BSP / or grid raycast):** stock DOOM walks a precompiled **BSP tree** front-to-back to find
   visible wall segments with no overdraw, clipping screen columns as it goes. *Tree-walking is pointer-heavy
   (~55@/node) — bad for FlipJump (G14)* → we start with a **grid raycaster** (cast one ray per screen
   column into a tile map; no BSP, no pointer chasing).
2. **Walls (`R_DrawColumn`):** for each visible column, distance → on-screen wall height via a **divide**
   (`scale ≈ projection / distance`), then draw a vertical strip. Textured = sample a texture column with a
   fixed step (`frac += step` each pixel, `texel = source[frac>>FRACBITS]`); flat = one shade.
3. **Floors/ceilings (`R_DrawSpan`):** drawn as horizontal spans, perspective-correct with a per-row distance
   (`yslope[]` table) and a divide per span. Flat = solid color (cheap).
4. **Sprites/things:** masked (transparent) columns, distance-sorted, clipped against walls.
5. **Lighting:** a **colormap** LUT chosen by distance + sector light level darkens texels.
All in 16.16 fixed-point, leaning on precomputed tables: `finesine/finecosine`, `finetangent`, `viewangletox`/
`xtoviewangle`, `yslope`, `distscale`, and `colormaps`. **Takeaway:** almost everything reduces to
table-lookups + a few fixed-point mul/div per column/span → exactly what fixed-address LUTs make cheap.

## Lever 0 — Pointer-free by compile-time unrolling (the "constant algorithm")

**The decisive realization (supersedes the U1 pointer-write problem):** a runtime pointer is only needed when
an address is computed *at runtime*. If instead we **unroll the work at compile time with `rep(n,i)`, every
address becomes a compile-time constant** → a **fixed-address write/read (~7@), never a ~1,300-op pointer
op.** We push the cost out of *time* and into *space* (assembled code size):

```
rep(W*H/PXPERWORD, i) calc_pixel SCREEN + i*WORD_STRIDE   // each i is constant ⇒ fixed address
```

- **Writes → fixed-address, packed.** Combine unrolling with **packed word writes** (8 px/word at 4bpp,
  w=32): ~768–1,536 fixed-address word-writes (~7@) ≈ **~150–290K ops** for a full 96×64 fill — *and zero
  pointers.* This replaces the whole fragile "sequential-pointer-write primitive" idea (old U1).
- **Branchless per-pixel selection.** Wall height per column is runtime, but we don't branch on it: unroll all
  `H` rows and have the tiny `calc_pixel` **select** ceiling/wall/floor color by comparing its (compile-time)
  `y` to the column's runtime top/bottom held in **fixed-address per-column scratch**. Same work every pixel,
  data-selected — the "constant algorithm." `calc_pixel` must be *tens of ops*, not hundreds.
- **The calling convention is the crux (settled in R1).** A *shared* `fcall` body cannot contain a
  per-call-site constant, so each unrolled stub must (a) write its compile-time `y` (or pre-staged per-row
  data) into a single fixed `CUR_Y` scratch — a constant-into-fixed-address write, a few ops — (b) `fcall`
  the shared compare/select/pack body, (c) write the result word to its constant `SCREEN+i*STRIDE` address.
  Per-stub space ≈ tens of ops; the heavy body exists once. Whether the compare lives in the stub (bigger
  stub, no `CUR_Y` write) or the body (smaller stub, one extra write) is R1's first experiment.
- **Share per-column, unroll per-pixel.** Unroll geometry once per column (`rep(W,x)`, 96×) and only the cheap
  fill per pixel — so the ~10K-op math (mul/div) happens ~96×, never 6144×. Each column's setup copies that
  column's top/bottom/color into the *single* fixed scratch the shared body reads — that's what keeps the
  shared body free of runtime-varying addresses.
- **Constant-table reads by runtime value → jump-table idiom, not pointers (kills old U3).** The tile map,
  textures, and colormaps are *constant data*. Read them by runtime value using FlipJump's **switch/jump-table
  pattern** (set a hex to the value, jump into `rep`-generated code at that offset — exactly how
  `stl/hex/mul.fj`'s `switch:`/`dst: ;switch` works). That reads a constant table by value **without a RAM
  pointer**. So the DDA map lookup is a value-indexed jump into a baked-map table, not a ~1,000-op pointer read.
- **Goal: zero runtime pointers in the whole game.** Writes = enumerated fixed addresses; data-dependent reads
  = value-indexed jumps into constant tables; per-frame runtime state = fixed-address scratch.
- **Fallback if unrolling overruns the compile budget:** a runtime loop over `hex.write_nth_*` /
  `ptr_index` (the 1.4.0 indexed macros — one copy of the body, O(w) per access). That re-buys pointer-class
  runtime cost (~8M/frame naive — likely fatal at 96×64), so the *real* fallback ladder is: shrink the stub →
  4bpp → smaller resolution → hybrid (unroll rows, loop columns) → indexed loop only for non-hot surfaces.

**The price is space-complexity, now the #1 engineering constraint (see TDD note).** Unrolling 6144 pixels ×
a fat macro would explode assembled size *and* compile time. The fix — **factor with `stl.fcall`/`stl.fret`,
don't inline:**
- A heavy `calc_pixel` body inlined 6144× = 6144×(its size). Instead, put the heavy, **address-independent**
  computation (LUT lookups, color select, word packing) in **one shared subroutine** invoked via
  **`stl.fcall` (≈`@-1` time, ~0 space) / `stl.fret` (≈1)** — *far* cheaper than `stl.call`'s stack-based
  ~2.5w@, and fine here because the per-pixel body is a **leaf** (no recursion ⇒ no stack needed; one
  `ret_reg` per nesting level if bodies ever chain — OQ9).
- What stays inlined per `rep` iteration is only a **tiny address-specific stub** (see the calling
  convention above). Per-iteration space drops from ~hundreds of ops to ~tens, while the heavy logic exists
  **once**. Unroll over **packed words** (≈768–1,536 iters, not 6,144) to shrink it further. The exact
  "shared vs inlined per pixel/word/column" factoring is settled empirically in R1 (it directly sets compile
  time).

## Fixed-address layout + precomputed tables (lever 1)

A **custom allocator reserves compile-time addresses** for hot data (or we statically allocate, no malloc),
operated on with `stl/hex` fixed macros:
- **`SCREEN`** — a **packed** `W*H`-pixel palette-index buffer (packed bits, not a strided `hex.vec`, so the
  device can read it directly — G18). Render write-target; device read-source.
- **Palette + colormap/light LUTs**; **`ylookup[]`/`columnofs[]`**; **reciprocal/trig/scale LUTs** (replace
  live division — G3/G15); per-column/span scratch; the **input mailbox**; the **sim state block** (player
  pos/angle/health, door states, entity slots — all fixed addresses).
Inner-loop indices are bounded (≤320, ≤200, ≤256 LUTs) ⇒ fixed-base + small-index injection ≈ a few @.

## Render core (clean flat/textured switch + extensible for HUD/text)

Mirror DOOM's `colfunc`/`spanfunc` indirection: one `draw_column`/`draw_span` macro whose body is chosen by
the `TEXTURED` flag (default flat). Only the innermost loop and the per-column data differ; everything
upstream is identical — no spaghetti.

**Built for later HUD/menu/text without rework:**
- **Compositor / pass pipeline.** `render_frame` is a fixed, ordered list of **passes**, each gated by a
  compile-time flag, all drawing into the same `SCREEN`:
  `render_3d_view` → `[render_sprites]` → `[render_statusbar]` → `[render_text/messages]` → `[render_menu]`.
  Minimal tier compiles **only** the first pass; later tiers flip flags on. Adding a pass = adding one
  gated macro call, never touching the 3D core.
- **Sub-window, not fullscreen, draw target.** The 3D view writes to a `(VIEW_X, VIEW_Y, VIEW_W, VIEW_H)`
  rectangle inside `SCREEN`, with overlay passes owning the rest — so HUD/text at higher res just claim screen
  rows the 3D view doesn't, no coordinate retrofit.
- **Reusable blitter primitive.** A general `blit_rect(src_addr, dst_x, dst_y, w, h, [transparent_idx])` is
  the shared backbone for sprites **and** HUD graphics **and** text glyphs — write it once (in R3 when first
  needed), and text/HUD/menu are all just callers.
- **Text = glyph LUT + blitter.** A `draw_string(x, y, ptr_to_chars)` walks bytes, indexes a **font glyph
  table** (fixed-address LUT, DOOM's `STCFN*`/`hu_font`), and calls `blit_rect` per glyph. **Stub the API
  now** (flag-gated, not compiled in minimal), so the seams exist from day one.
- **Cost note:** these overlay passes are themselves **pointer-write-heavy** (same framebuffer-write cost as
  walls), so they're enabled only at tiers whose budget/res can pay for them — the architecture is free, the
  pixels are not.

## Game simulation — the other half (was missing from this plan)

Rendering alone is a screensaver. The *game* needs per-tic simulation, and it shares the same 1M-op frame
budget. The good news: on a tile map, sim work is **tile lookups + signed compares + adds** — exactly the
cheap class — so sim is budgeted, not feared. Scope ladder (compile-time-gated like render passes):

- **S0 — Walk (ships with R2, part of the minimal tier).** Read mailbox → turn (angle ± const, table-wrapped)
  → strafe/forward (pos += dir·speed, `×const`→shifts) → **collision**: check the 1–2 destination tiles
  (value-indexed map lookup, same idiom as the raycaster's DDA) with axis-separated slide (DOOM-feel wall
  glide, not full `P_TryMove`). Cost: a handful of lookups + adds ≈ **a few K ops/tic** — noise.
- **S1 — Doors + hitscan (minimal tier, after S0).** Use-key opens a door tile (timed state in a small
  fixed-address door table, re-bake-free: door tiles are map *values* the DDA jump-table already
  distinguishes); fire = one extra DDA ray (same code as a render column) → hit wall/entity. ≈ one column's
  worth of ops/shot.
- **S2 — Entities (very-good tier, with sprites).** N fixed entity slots (pos, type, hp, state) at fixed
  addresses — *no* DOOM thinker lists (pointer-chasing). Per tic per entity: line-of-sight ray (DDA), chase
  step (tile-collision like the player), melee/hitscan damage. Budget ~O(few columns) per active entity;
  cap actives per tic (round-robin) to bound the frame.
- Health/armor/ammo = fixed-address counters touched by S1/S2 events. Level exit = a tile value. Game over /
  restart = re-init state block. **No** savegames, no demos-from-WAD, no multiplayer, no audio (G9).

**Frame budget split (the ledger R1/R2 must respect, at 10M fj/s, 10fps):**

| Slice | Budget |
|---|---|
| Framebuffer fill (Lever-0, packed) | ~300K |
| Per-column geometry (96 cols, LUT+DDA, mul/div-free) | ~300K |
| Sim tic (S0–S1) + input poll | ~100K |
| Device IO signaling + frame overhead | ~50K |
| **Reserve** (overdraw, sprites later, estimate error) | **~250K** |

## Devices & interpreter IO

- **`InMemoryScreen256` (output device) — headless-first.** We develop in cloud/CI sessions with no display,
  so the device's *primary* backend writes frames to disk (PNG per frame / animated GIF / video via ffmpeg,
  plus a **frame-hash log** for golden tests); an interactive window (pygame or similar, as an optional
  `flipjump[screen]` extra) is a second backend behind the same interface. The FJ program outputs a **small
  enum** selecting a function, then the argument(s) as raw output bits; the device reads the referenced
  buffer from interpreter memory:
  - **`init_screen(w, h, bpp, palette_size)`** — first call; sizes the window, the packed framebuffer
    stride (4bpp or 8bpp), and the palette table (program-driven, not hard-coded at device construction).
  - `set_palette(palette_fj_address)` — read `palette_size`×RGB from that address.
  - `update_screen(screen_fj_address)` — read `W*H` packed indices, expand via palette, upscale, present.
  - `update_rectangle(w, h, rect_fj_address)` — partial update.
  Per-frame FJ cost ≈ emitting one enum + one address (~a few @) → blit ≈ free. The device timestamps
  presents, so measured fps comes from the device log, not hand-timing.
- **Keyboard (input device), non-blocking, virtual time, scriptable.** Host writes key events into a fixed
  **mailbox** region the FJ program polls once/tic (or a stream fallback: `0x00`=NO_EVENT, else
  `[0xFE][down/up][keycode]`; idle never EOFs). The event *source* is pluggable: live keys (interactive
  backend) **or a scripted event file** (`tic_number, down/up, keycode` lines) — the scripted source is what
  makes end-to-end tests and CI deterministic (our equivalent of DOOM's demo playback). **No timer device** —
  the FJ program keeps its own frame-counter clock.
- **Interpreter memory-access hook (MP-B).** InMemoryScreen256 and the mailbox need a **new device↔memory
  hook** (read `SCREEN[]`/palette by address; write the mailbox) beyond the per-bit `read_bit`/`write_bit`
  (G17). Invocation: `fj --di keyboard --do InMemoryScreen256 doom.fjm`.

## Prerequisite middle-projects (before any doom-fj code)

- **MP-A · Interpreter speedup (gating).** (0) **Record the 1.4.0 baseline first** — `prime_sieve.fj` fj/s
  on this hardware, w=32 and w=64, so every change has a denominator. (1) **Segment-aware memory** — a
  single flat `bytearray` can't model FlipJump's sparse segments; use per-segment dense buffers (and keep
  our hand-written layout's addresses compact so dense storage stays cheap). (2) **Fast-run mode** that
  turns off the slow, optional features: per-op **statistics** (`op_counter`/flip/jump/`register_op`),
  **trace**, **breakpoints**/debug-ops-list, and hoists the per-op **IO-range check** out of the hot path.
  (3) **Decoded-word cache** — FlipJump self-modifies (`wflip` rewrites the jump words of *future* ops,
  anywhere in code space, not just "near `ip`"), so invalidation must be **address-keyed: any flip landing
  in a cached word invalidates that entry** (e.g. a dirty-check on the flip address against the cache's
  address range/tags). The check itself sits on the hot path — its overhead must be benchmarked, not
  assumed. Benchmark **`prime_sieve.fj`**; test **w=32 vs w=64**; target ≥10M fj/s. **Correctness gate: the
  full 1,029-program catalog (`pytest --catalog`) stays green under fast-run mode + cache** — this suite
  exercises self-modifying code, pointers, and IO far beyond any hand-picked test. **Fallback if CPython
  plateaus:** move the loop to a C-extension/Cython or PyPy, keeping devices + debugger intact.
- **MP-B · CLI device-IO + devices.** `fj --di/--do` flags, the device↔memory hook, and the
  `InMemoryScreen256` (headless + interactive backends) + `Keyboard` (scripted + live sources) devices —
  each TDD'd (golden frame-hash tests; scripted-input replay tests).
- **MP-C · CLI debugger (parallel, non-gating).** Convert `fj --run`'s debugger from GUI to CLI
  (headless/agent-usable). Valuable for debugging the renderer later, but nothing in R0–R1 blocks on it —
  schedule it alongside, not in front.

## Milestones

1. **MP-A → MP-B** (gating, in order); **MP-C** in parallel (each fully tested).
2. **R0 — Asset & table pipeline:** WAD→`.fj` fixed data tables (palette, per-surface flat colors,
   textures/map geometry); generate reciprocal/trig/scale/colormap LUTs. **Includes the gridification step
   (new):** DOOM levels are vertex/linedef/sector geometry, *not* a grid — the converter must rasterize a
   level (E1M1) onto a tile map (tile size ~64 map units; door/exit linedefs → special tile values; sector
   floor colors → per-tile surface colors). **Risk: gridded E1M1 may be unrecognizable** (diagonal walls,
   varying floor heights are lost) — fallback is hand-authoring a DOOM-*flavored* grid map using real DOOM
   palette/colors (and saying so honestly). Use the **shareware `doom1.wad`** for development; **Freedoom**
   assets for anything we redistribute (e.g. test fixtures in the repo).
3. **R1 — Vertical slice (de-risk):** **first** prove **Lever 0** — a tiny `rep`-unrolled, packed,
   fixed-address `calc_pixel` fill + a value-indexed jump-table map read — settle the **stub↔fcall calling
   convention**, and **measure both ops/frame AND assemble time** at a small size, then extrapolate to
   96×64 (U0). If compile time explodes, dial back unrolling before writing the renderer. Then a **16×16
   flat room**, hand-FJ grid raycaster (DDA via baked-map jump-table, geometry per-column) → `SCREEN` →
   InMemoryScreen256 (headless backend) → scripted-key movement. End-to-end before scaling. **This is the
   go/no-go gate (U0–U2, U4).**
4. **R2 — Minimal tier:** scale to 96×64, flat-shaded walls+floors+ceilings, real (gridified) map geometry,
   **S0 walk+collide sim**, ~10fps. Then **S1 doors+hitscan** within the same budget ledger.
5. **R3 — Scale up:** raise res tier-by-tier; enable `TEXTURED`, sprites + **S2 entities** as interpreter
   speed allows; reassess budget at each (fallback ladder in Lever 0).

## ⚠️ Biggest unknowns — things that may not work / cost much more than first discussed

These are the honest, *quantified* "this could blow up" risks. R1's whole job is to measure them before we
commit to scaling. Ranked by how much they threaten the project:

- **U0 — Space-complexity / compile time is the #1 risk.** Lever 0 trades runtime pointer cost for
  *assembled code size*: unrolling ~768–1,536 word-stubs (and switch-tables for map/textures/colormap) can
  make the **assembler** take minutes per build and balloon the `.fjm`. A slow compile **destroys the TDD
  loop** — and this is now *measured*, not speculative: **243 unrolled O(w) indexed reads ≈ 7-minute
  assemble** (catalog campaign). Our stubs are O(1)-size (not O(w)), which is exactly why the rule "only
  O(1) bodies unroll" exists — but the total at 96×64 is still unproven. **Primary mitigation:
  `stl.fcall`/`stl.fret` factoring** — heavy logic lives once, only a tiny stub inlines per iteration. Plus:
  packed words (768 @ 4bpp), share geometry per-column, bound table sizes, and **measure assemble time as a
  first-class metric from R1.** *Unknown:* the achievable per-iteration stub size and total compile time at
  96×64 — if still too big, walk the fallback ladder (Lever 0).
- **U1 — (was highest; now largely mitigated by Lever 0).** Compile-time-unrolled, packed, fixed-address
  writes make the framebuffer fill ~150–290K ops/frame with no pointers — *provided* U0 stays in check.
  Residual risk lives in U0, not here.
- **U2 — A single 32-bit multiply is ~10,000 ops; a divide ~68,000.** "A few FixedMuls/divides per column"
  silently = several × the frame budget. The hot path must be **mul/div-free** (reciprocal/scale LUTs +
  DDA incremental adds + `×constant`→shifts + 8.8/narrow math). *Unknown:* whether the raycaster's
  perspective/scale math fully reduces to LUT lookups + adds for *our* geometry, or leaves residual
  per-column mul/div we can't strength-reduce.
- **U3 — (was a pointer read; now mitigated).** The DDA map lookup is a **value-indexed jump into a baked-map
  switch-table** (Lever 0), not a ~1,000-op RAM pointer read. Residual cost is the jump-table dispatch + its
  contribution to compile size (folds into U0), and the DDA's variable step count (fps swing, G13).
- **U4 — 10M fj/s in CPython is unproven.** 2M→10M is 5×; the decoded-word cache + fast-mode may not get
  there in pure Python — and the cache's **address-keyed invalidation check is itself hot-path overhead**
  that eats into the win. Fallback: C-extension/Cython/PyPy (schedule + complexity risk). Everything above
  assumes 10M; at 2M the budget is 200K ops/frame and *even flat 96×64 is out of reach* without the speedup.
- **U5 — 32×32→64 fixed-point intermediates.** A 16.16 `FixedMul` needs a 64-bit product (two words at
  w=32) with hand-carried overflow, adding ops and macro complexity beyond a plain `hex.mul`. Pushes us
  toward 8.8 where precision allows — but 8.8 may cause visible wobble/jitter in angles/distances (quality
  risk, not just speed).
- **U6 — Variable shifts are pointer-class expensive.** Constant shifts (`frac>>FRACBITS`) are ~free, but any
  **runtime-amount** shift (e.g. normalizing a distance to index a reciprocal LUT) is expensive. LUT designs
  must be indexable **without** a runtime shift (e.g. integer-distance index), or they quietly cost like
  pointers.
- **U7 — `@` itself grows with total program size (G11).** Every cost above scales with `@≈27`, but a
  DOOM-scale program (huge LUTs + textures + map) could push `@` higher, inflating *all* per-op costs
  uniformly. Big static tables aren't free even when "fixed-address."
- **U8 — Assemble time/memory at DOOM scale (G20).** Mega-tables (colormaps, textures, font, map) may make
  the *assembler* (not just runtime) slow or memory-hungry; unvalidated at this scale.
- **U9 — Divide is even worse than multiply, and lighting can secretly go per-pixel.** `hex.div` is a
  long-division loop ⇒ **≥ a multiply (~10K+ ops)**, so the plan must replace **every** runtime divide with a
  reciprocal/scale LUT — not just the obvious ones. Separately, applying DOOM's distance **colormap** naively
  is a **pointer read per pixel** (~1,000 ops × 6144 ≈ another ~6M/frame). Mitigation: in flat mode compute
  the shaded color **once per column** (one colormap lookup, then fill), never per pixel — lighting must
  live in the per-column setup, not the inner write loop.
- **U10 — Framebuffer clear.** A full per-frame `SCREEN` clear would be another ~W·H pointer writes (~8M).
  The grid raycaster avoids it *only because every column is drawn ceiling→wall→floor top-to-bottom with no
  gaps* — so "no clear" is a **load-bearing invariant**, not a convenience. Any partial-draw path (sprites
  with gaps, letterboxed view) reintroduces a clear cost and must be handled with explicit fills, not a
  blanket clear.
- **U11 — Gridification fidelity (new).** Rasterizing E1M1's linedef geometry onto a tile grid may yield an
  unrecognizable level (diagonals staircased, height variation flattened, secret areas broken). If so, the
  honest deliverable shifts to "DOOM-flavored levels with real DOOM assets" — a *presentation* risk, not a
  technical one, but it changes what we can claim. Settled in R0 by eyeballing the converted map.

**Net:** *flat, low-res, pointer-free (unrolled + packed), mul/div-free* DOOM with **S0–S1 gameplay** is the
realistic near-term target, hinging on **U0 (compile time) + U2 (mul/div elimination) + U4 (interpreter
speed)**. **Textured / HUD-text / higher-res / S2 entities are genuinely uncertain** — gated on both a
faster interpreter *and* compile-time headroom — promised only as flag-gated stretch goals. **R1's measured
ops/frame *and* assemble time are the joint go/no-go gate before any scaling.**

## Gaps, risks & scope cuts (resolved + tracked)

- **G1 — engine scope:** *resolved* → hand-written FlipJump, grid raycaster first, no c2fj.
- **G2 — blit cost:** *resolved* → `InMemoryScreen256` (device reads framebuffer from memory) ⇒ ~0 FJ ops.
- **G3/G15 — divides & fixed-point mul:** precomputed reciprocal/scale LUTs; fixed-point only (no FP);
  consider 8.8 precision where 16.16 is overkill; budget `FixedMul`/divide counts explicitly.
- **G4 — timing:** *resolved* → virtual frame-counter clock; no timer device.
- **G5 — memory model:** segment-aware dense storage; compact hand-written address layout.
- **G6 — interpreter fast-mode:** strip stats/trace/breakpoints, hoist IO check; C-ext/PyPy fallback.
- **G7 — WAD asset pipeline:** R0 converter; per-surface flat-color LUT for flat mode; **gridification +
  asset licensing (shareware vs Freedoom) — see R0 / U11.**
- **G8/G23 — tiny-res features:** minimal tier = **3D view only**; auto-start a level (`-warp`-equivalent),
  **skip menu/HUD/text**; these return at higher tiers (per-tier table above).
- **G9 — audio:** out of scope (no audio device).
- **G11 — `@` grows with program size** (~25–30 at DOOM scale): keep hot data at low, `pad`-aligned
  addresses; treat the 1M budget as `@`-sensitive and re-measure.
- **G13 — variable frame cost:** floors/ceilings + sprites are scene-dependent → fps swings; set a worst-case
  scene target or accept variable fps.
- **G14 — BSP is pointer-heavy:** prefer the grid raycaster; real-DOOM BSP only if node counts stay low.
- **G16 — cache coherence with self-modifying code:** *criterion corrected* — invalidate **address-keyed on
  any flip into a cached word** (`wflip` targets future ops anywhere, not just "near `ip`"); benchmark the
  dirty-check overhead (MP-A).
- **G17 — device↔memory hook:** new interpreter API in MP-B (read for screen/palette, write for mailbox).
- **G18 — framebuffer layout:** `SCREEN` is a packed buffer (4bpp/8bpp knob), device-readable directly.
- **G20 — assembler scalability:** zero-fill segments for big zeroed buffers; watch assemble time/memory.
- **G21 — tic/render decoupling:** allow rendering 1 of N tics so input/sim stay responsive.
- **G22 — flat-mode reference:** build a host-side reference model of our *exact* flat renderer + sim to
  diff `SCREEN→PNG` (and state) against (stock DOOM only references textured mode).
- **G25 — game simulation scope (new, was wholly missing):** S0/S1/S2 ladder + budget ledger (see the Game
  simulation section); no savegames/demos/multiplayer.
- **G26 — headless-first devices (new):** development happens in cloud sessions with no display; file-based
  frame output + frame hashes are the primary screen backend, scripted key events the primary input source;
  interactive backends are optional extras.

## Open questions (still unresolved — to be settled by measurement or in R1)

These are genuinely undecided; most are *empirical gates*, not design debates:

- **OQ1 — Interpreter speed (U4):** does CPython + segment memory + decoded-word cache (including its
  invalidation overhead) reach **10M fj/s**, or do we need the C-extension/PyPy fallback? Decided in
  **MP-A** by benchmark against the recorded 1.4.0 baseline.
- **OQ2 — `w=32` vs `w=64`:** decided in **MP-A** by benchmark (speed + memory + pointer/`@` cost). The
  skill's guidance and catalog measurements already favor w=32; MP-A confirms at DOOM scale.
- **OQ3 — Lever-0 compile cost (U0) + the calling convention:** stub-side vs body-side compare, per-word vs
  per-pixel stubs, and whether the `fcall`-factored 96×64 build compiles fast enough to keep the TDD loop
  alive. Settled in **R1**; drives whether we keep full unrolling or walk the fallback ladder.
- **OQ4 — Can the per-column math be made fully mul/div-free (U2)?** Whether the raycaster's
  perspective/scale/height math reduces entirely to integer-indexed LUTs + DDA adds + `×const`→shifts for our
  geometry, or leaves residual runtime `FixedMul`/`FixedDiv` we must budget. Settled in **R1**.
- **OQ5 — Precision: 16.16 vs 8.8 (vs mixed):** where can we drop to 8.8/narrow (≈4× cheaper mul) **without**
  visible angle/distance wobble? Per-quantity decision, validated against the reference model.
- **OQ6 — Decoded-word cache invalidation design (G16):** address-keyed dirty-check mechanism (tag check per
  flip? page-granular invalidation?) and its measured hot-path overhead. Settled in **MP-A**, gated by the
  full catalog staying green.
- **OQ7 — Device↔memory hook API shape (G17):** exact interface for a device reading `SCREEN`/palette by
  address and writing the keyboard mailbox. Settled in **MP-B**.
- **OQ8 — Map/texture as baked switch-tables:** is the value-indexed jump-table for the tile map (and later
  textures) small enough to stay within the compile budget (ties to U0)? Map: likely yes; **textures: open**.
- **OQ9 — `fcall` non-reentrancy:** `fcall`'s single `ret_reg` can't nest — each nesting level needs its own
  `ret_reg` (skill-confirmed), and recursion needs the heavier `stl.call`/stack (~2.5w@). Does any hot-path
  call chain exceed one level? TBD as the renderer's call graph takes shape in R1.
- **OQ10 — Variable frame cost / fps (G13):** accept variable fps, or target a worst-case scene and cap?
  Decided once R2 has real geometry.
- **OQ11 — Scope reach:** whether **textures, sprites, HUD/menu/text, S2 entities, and higher resolution**
  are achievable at all is gated on OQ1+OQ3 headroom — they remain flag-gated stretch goals, not
  commitments, until R1/R2 numbers are in.
- **OQ12 — 4bpp vs 8bpp (new):** does flat minimal fit in 16 colors (halving fill cost), or does
  distance-lighting need more shades? Decided in R1/R2 against the reference model's output.
- **OQ13 — Gridification fidelity (U11, new):** does converted E1M1 read as E1M1? Decided in R0 by
  inspection; fallback = hand-authored DOOM-flavored maps.

## Verification

- **TDD everywhere:** a unit test per FlipJump macro (FixedIO, `--werror`, boundary inputs per behavior
  path), per interpreter change, per device. Macro tests follow the catalog's registration pattern so they
  run under the existing test infra.
- **MP-A:** baseline-vs-after `prime_sieve.fj` fj/s (target ≥10M, else fallback); **the full 1,029-program
  catalog green under fast-run + cache** (the real correctness gate for self-modifying-code edge cases);
  DOOM-scale **memory-footprint** test; w=32 vs w=64 comparison.
- **Render budget + compile budget:** track **both** `op_counter`/frame (≤1M total per the budget ledger)
  **and assemble time + `.fjm` size** at every milestone. Micro-benchmark in isolation first: a Lever-0
  unrolled packed fill (U0/U1) and a per-column-math path (U2) — confirm fixed-address unrolled writes are
  ~7@ (vs a pointer-based control), the hot path is mul/div-free, and the unrolled build compiles fast
  enough to keep the TDD loop alive — before trusting any tier promise.
- **Correctness:** `SCREEN→PNG` dump (headless device backend) hashed + diffed against the host-side flat
  reference model — renderer *and* sim state (player pos/angle after a scripted input sequence must match
  the reference exactly); per-region op-count profiling (profiling mode keeps stats on; fast-run strips
  them).
- **End-to-end:** `fj --di keyboard --do InMemoryScreen256 doom.fjm` with a **scripted key-event file** —
  scene renders, movement/collision/fire behave per the reference, measured fps (device present-log) meets
  the active tier; capture video (headless backend) as the deliverable.
