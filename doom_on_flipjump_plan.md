# Running DOOM on FlipJump — Plan

## Context & key decisions

FlipJump is a one-instruction language ("flip a bit, then jump"). Goal: run DOOM on it, driven by our own
screen + keyboard devices, as fast as we can. Two facts dominate every decision:

1. **Pointer accesses cost ~37–55× a fixed-address access** (a runtime pointer must rebuild its `w`-bit
   address bit-by-bit into a flip-opcode; the `w(0.5@+2)` term ≈ ~32@ at w=64, vs ~@ for a compile-time-fixed
   address — `stl/hex/pointers/xor_to_pointer.fj:32`). So **everything heavy must live at compile-time-fixed
   addresses**, accessed `hex.hex`-style, never `hex.pointers`.
2. **The interpreter is the gate.** Today `fj --run` is ~2M fj/s; we target **10M fj/s** and budget **≤1M
   fj-ops/frame** (10M ÷ 10fps). All math respects that ceiling.

**Resolved this round:**
- **No c2fj.** The whole engine is **hand-written FlipJump on fixed addresses** — c2fj's pointer-based RISC-V
  memory can't fit the budget anyway. We use DOOM's *source as the algorithm reference* and reimplement its
  renderer by hand (raycaster-grade first, scaling toward DOOM fidelity). (Removes the old c2fj bridge /
  version-compat risks.)
- **No floating point.** DOOM is entirely **16.16 fixed-point integer** math — FlipJump needs *no* FP. We add
  fixed-point helpers (`FixedMul`/`FixedDiv`) built on `hex.mul`/`hex.div` + **reciprocal/trig LUTs**; no FP
  in the 1.5.0 stl.
- **Target `w=32`** (validate in MP-A): DOOM's fixed-point is 32-bit, so one word = one number; halving word
  width roughly halves address/pointer cost and the per-op data the interpreter moves (likely faster). Fall
  back to w=64 only if memory-constrained.
- **No timer device** (see G4): time is a virtual clock = internal frame counter; the sim advances one tic
  per rendered frame. Deterministic, and no real-time input needed.

## Methodology: test-driven, every macro in isolation

**Non-negotiable:** unit-test **every** FlipJump macro the moment it's written, in a sterile environment,
using the repo's test framework + `FixedIO` (compare exact output for crafted inputs). Same for the new
1.5.0 interpreter changes and the Screen/Keyboard devices. Write the test first, then the macro. Bugs in
hand-written FlipJump are brutal to find later — TDD up front is the only sane path.

**Keep compile+run fast (point 3) — it's a hard methodology constraint, not a nicety.** Lever 0 trades
runtime for assembled size, so a careless macro makes every build take minutes and the TDD loop collapses.
Rules: **factor heavy bodies into one `stl.fcall`/`stl.fret` leaf subroutine** (≈`@-1`/≈1, ~0 extra space)
instead of inlining them across a `rep`; keep the inlined per-iteration stub to *a few* ops; share geometry
per-column (unroll 96×, not 6144×); test macros on tiny inputs (a 4×4 fill, not 96×64) so unit builds stay
sub-second; and treat **assemble time + `.fjm` size as tracked metrics** alongside ops/frame, starting at R1.

## Versioning & tooling

Baseline **`fj==1.4.0`** (deployed before we code) → all work ships as **`fj==1.5.0`**. Every hand-written
`.fj` file uses the **flipjump-writing skill** and leans on the **~1000 example programs** as patterns rather
than inventing macros.

## Goals (tiers) & per-tier feature support

| Tier | Res | FPS | Realistic feature set |
|------|-----|-----|------------------------|
| **Minimal (primary)** | 96×64 | 10 | **3D view only**, flat-shaded walls/floors/ceilings. **No** HUD/status-bar, menu, on-screen text, or textures; sprites minimal/none. |
| Very good | 160×100 | 15 | textures on (if speed allows), basic sprites, maybe a reduced HUD; small but readable. |
| Best | 320×200 | 25 | full textures, sprites, HUD/text; needs substantially more fj/s. |

`RESX`/`RESY` and a `TEXTURED` flag (default `0`=flat) are compile-time knobs. At 96×64 the 320-px-wide HUD,
menus, and text simply don't fit — hence "3D view only" for the minimal tier. Features return as resolution
and budget grow.

## What 1M ops/frame buys (the *corrected*, honest reckoning)

⚠️ **An earlier version of this plan under-counted the framebuffer write by ~6×.** The real stl costs
(measured, w=32, `@≈27` at DOOM scale) are the foundation, so state them plainly:

| Operation | stl macro | cost (w=32) | ≈ fj-ops |
|-----------|-----------|-------------|----------|
| Fixed-address byte write | `hex.xor`-style | ~7@ | ~190 |
| **Pointer** byte write (runtime addr) | `write_byte` | 41@+197 | **~1,300** |
| **Pointer** byte read (runtime addr) | `read_byte` | ~33@ | **~1,000** |
| 32-bit multiply (8 hex) | `hex.mul` | ~350@+1300 | **~10,000** |
| 16-bit multiply (8.8, 4 hex) | `hex.mul` | ~88@+320 | **~2,700** |
| 32-bit **divide** (8 hex) | `hex.div` | ~2300@+6400 | **~68,000** |
| constant shift (`<<`/`>>` by const) | `hex.shl_hex` | ~8@+32 | **~250** |

**The trap:** a framebuffer pixel at runtime `(x,y)` lives at a runtime-computed address, so each pixel write
is a **pointer write (~1,300 ops), not the ~190-op fixed write I first assumed.** Naively: 6144 px × ~1,300 ≈
**~8M ops/frame just for writes** — ~8× over the *entire* 1M budget. InMemoryScreen256 makes the *blit* free
but does **nothing** for the cost of *constructing* the framebuffer in memory. This single correction is the
biggest change to the plan.

**The mitigation (see Lever 0): compile-time unrolling + packed words ⇒ fixed-address, pointer-free.**
- Unroll the framebuffer fill with `rep` so every address is a compile-time constant ⇒ **~7@ fixed write,
  not ~41@ pointer write.** Combine with **packed words** (4 px/word at 8bpp,w=32): ~1,536 word-writes ×
  ~7@ ≈ **~290K ops/frame** for a full flat 96×64, with **zero pointers** and no fragile runtime
  address-advance primitive. This is the real fix for old-U1.
- This is *why* DOOM draws memory-coherent columns/spans — we go further and make the runs **compile-time
  constant**.

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
- **Cheaper multiplies (point 2):** (a) **strength-reduce `×constant` to constant shifts+adds** (~250 ops
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

- **Writes → fixed-address, packed.** Combine unrolling with **packed word writes** (4 px/word at 8bpp,w=32):
  ~1,536 fixed-address word-writes (~7@) ≈ **~290K ops** for a full 96×64 fill — *and zero pointers.* This
  replaces the whole fragile "sequential-pointer-write primitive" idea (old U1).
- **Branchless per-pixel selection.** Wall height per column is runtime, but we don't branch on it: unroll all
  `H` rows and have the tiny `calc_pixel` **select** ceiling/wall/floor color by comparing its (compile-time)
  `y` to the column's runtime top/bottom held in **fixed-address per-column scratch**. Same work every pixel,
  data-selected — the "constant algorithm." `calc_pixel` must be *tens of ops*, not hundreds.
- **Share per-column, unroll per-pixel.** Unroll geometry once per column (`rep(W,x)`, 96×) and only the cheap
  fill per pixel — so the ~10K-op math (mul/div) happens ~96×, never 6144×.
- **Constant-table reads by runtime value → jump-table idiom, not pointers (kills old U3).** The tile map,
  textures, and colormaps are *constant data*. Read them by runtime value using FlipJump's **switch/jump-table
  pattern** (set a hex to the value, jump into `rep`-generated code at that offset — exactly how
  `stl/hex/mul.fj`'s `switch:`/`dst: ;switch` works). That reads a constant table by value **without a RAM
  pointer**. So the DDA map lookup is a value-indexed jump into a baked-map table, not a ~1,000-op pointer read.
- **Goal: zero runtime pointers in the whole game.** Writes = enumerated fixed addresses; data-dependent reads
  = value-indexed jumps into constant tables; per-frame runtime state = fixed-address scratch. (Point 5.)

**The price is space-complexity, now the #1 engineering constraint (see TDD note).** Unrolling 6144 pixels ×
a fat macro would explode assembled size *and* compile time. The fix — **factor with `stl.fcall`/`stl.fret`,
don't inline:**
- A heavy `calc_pixel` body inlined 6144× = 6144×(its size). Instead, put the heavy, **address-independent**
  computation (LUT lookups, color select, word packing) in **one shared subroutine** invoked via
  **`stl.fcall` (≈`@-1` time, ~0 space) / `stl.fret` (≈1)** — *far* cheaper than `stl.call`'s stack-based
  ~2.5w@, and fine here because the per-pixel body is a **leaf** (no recursion ⇒ no stack needed).
- What stays inlined per `rep` iteration is only a **tiny address-specific stub**: a fixed-address write of
  the shared result to `SCREEN + i*STRIDE` (cheap in *both* time and space) + the `fcall`. So per-iteration
  space drops from ~hundreds of ops to ~tens, while the heavy logic exists **once**.
- **The split is the design crux:** the *write address* must stay compile-time-constant (so it's a fixed-
  address write, not a pointer), so it can't be hidden inside the shared body — only the *value* computation
  shares. Unroll over **packed words** (≈1,536 iters, not 6,144) to shrink it further. The exact "shared vs
  inlined per pixel/word/column" factoring is settled empirically in R1 (it directly sets compile time).

## Fixed-address layout + precomputed tables (lever 1)

A **custom allocator reserves compile-time addresses** for hot data (or we statically allocate, no malloc),
operated on with `stl/hex` fixed macros:
- **`SCREEN`** — a **packed** `W*H`-byte palette-index buffer (packed bits, not a strided `hex.vec`, so the
  device can read it directly — G18). Render write-target; device read-source.
- **Palette + colormap/light LUTs**; **`ylookup[]`/`columnofs[]`**; **reciprocal/trig/scale LUTs** (replace
  live division — G3/G15); per-column/span scratch.
Inner-loop indices are bounded (≤320, ≤200, ≤256 LUTs) ⇒ fixed-base + small-index injection ≈ a few @.

## Render core (clean flat/textured switch + extensible for HUD/text)

Mirror DOOM's `colfunc`/`spanfunc` indirection: one `draw_column`/`draw_span` macro whose body is chosen by
the `TEXTURED` flag (default flat). Only the innermost loop and the per-column data differ; everything
upstream is identical — no spaghetti.

**Built for later HUD/menu/text without rework (per the request to keep it easy to extend):**
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
  table** (fixed-address LUT, DOOM's `STCFN*`/`hu_font`), and calls `blit_rect` per glyph. Menus/stats are
  then just `draw_string` + `blit_rect` calls. **Stub the API now** (flag-gated, not compiled in minimal),
  so the seams exist from day one even though no text ships until the resolution supports it.
- **Cost note:** these overlay passes are themselves **pointer-write-heavy** (same framebuffer-write cost as
  walls), so they're enabled only at tiers whose budget/res can pay for them — the architecture is free, the
  pixels are not.

## Devices & interpreter IO

- **`InMemoryScreen256` (output device).** The FJ program outputs a **small enum** selecting a function, then
  the argument(s) as raw output bits; the device reads the referenced buffer from interpreter memory:
  - **`init_screen(w, h, palette_size)`** — first call; sizes the window, the packed framebuffer stride, and
    the palette table (so `W`/`H`/`palette_size` are program-driven, not hard-coded at device construction).
  - `set_palette(palette_fj_address)` — read `palette_size`×RGB from that address.
  - `update_screen(screen_fj_address)` — read `W*H` packed indices, expand via palette, upscale, present.
  - `update_rectangle(w, h, rect_fj_address)` — partial update.
  Per-frame FJ cost ≈ emitting one enum + one address (~a few @) → blit ≈ free.
- **Keyboard (input device), non-blocking, virtual time.** Host writes key events into a fixed **mailbox**
  region the FJ program polls once/tic (or a stream fallback: `0x00`=NO_EVENT, else `[0xFE][down/up][keycode]`;
  idle never EOFs). **No timer device** — the FJ program keeps its own frame-counter clock.
- **Interpreter memory-access hook (MP-B).** InMemoryScreen256 and the mailbox need a **new device↔memory
  hook** (read `SCREEN[]`/palette by address; write the mailbox) beyond the per-bit `read_bit`/`write_bit`
  (G17). Invocation: `fj --di keyboard --do InMemoryScreen256 doom.fjm`.

## Prerequisite middle-projects (before any doom-fj code)

- **MP-A · Interpreter speedup (gating).** (1) **Segment-aware memory** — a single flat `bytearray` can't
  model FlipJump's sparse segments (G5/point 3); use per-segment dense buffers (and keep our hand-written
  layout's addresses compact so dense storage stays cheap). (2) **Fast-run mode** that turns off the slow,
  optional features (G6): per-op **statistics** (`op_counter`/flip/jump/`register_op`), **trace**,
  **breakpoints**/debug-ops-list, and hoists the per-op **IO-range check** out of the hot path. (3) **Decoded-
  word cache** — but FlipJump self-modifies via `wflip` into padded space, so the cache **must invalidate on
  writes near `ip`** (G16, correctness-critical). Benchmark **`prime_sieve.fj`**; test **w=32 vs w=64**;
  target ≥10M fj/s. **Fallback if CPython plateaus:** move the loop to a C-extension/Cython or PyPy, keeping
  devices + debugger intact (G6/old).
- **MP-B · CLI device-IO + devices.** `fj --di/--do` flags, the device↔memory hook, and the
  `InMemoryScreen256` + `Keyboard` devices — each TDD'd.
- **MP-C · CLI debugger.** Convert `fj --run`'s debugger from GUI to CLI (headless/agent-usable).

## Milestones

1. **MP-A / MP-B / MP-C** as above (each fully tested).
2. **R0 — Asset & table pipeline:** WAD→`.fj` fixed data tables (palette, per-surface flat colors, textures/
   map geometry); generate reciprocal/trig/scale/colormap LUTs.
3. **R1 — Vertical slice (de-risk, G24):** **first** prove **Lever 0** — a tiny `rep`-unrolled, packed,
   fixed-address `calc_pixel` fill + a value-indexed jump-table map read — and **measure both ops/frame AND
   assemble time** at a small size, then extrapolate to 96×64 (U0). If compile time explodes, dial back
   unrolling before writing the renderer. Then a **16×16 flat room**, hand-FJ grid raycaster (DDA via
   baked-map jump-table, geometry per-column) → `SCREEN` → InMemoryScreen256 window → key-move. End-to-end
   before scaling. **This is the go/no-go gate (U0–U2, U4).**
4. **R2 — Minimal tier:** scale to 96×64, flat-shaded walls+floors+ceilings, real map geometry, ~10fps.
5. **R3 — Scale up:** raise res tier-by-tier; enable `TEXTURED` and sprites as interpreter speed allows;
   reassess budget at each (fallback tree in G24).

## ⚠️ Biggest unknowns — things that may not work / cost much more than first discussed

These are the honest, *quantified* "this could blow up" risks. R1's whole job is to measure them before we
commit to scaling. Ranked by how much they threaten the project:

- **U0 — Space-complexity / compile time is now the #1 risk (point 3).** Lever 0 trades runtime pointer cost
  for *assembled code size*: unrolling 6144 pixels (and switch-tables for map/textures/colormap) can make the
  **assembler** take minutes per build and balloon the `.fjm`. A slow compile **destroys the TDD loop**.
  **Primary mitigation: `stl.fcall`/`stl.fret` factoring** — heavy logic lives once, only a tiny stub inlines
  per iteration (see Lever 0). Plus: unroll over packed words (~1,536, not 6,144), share geometry per-column,
  bound table sizes, and **measure assemble time as a first-class metric from R1.** *Unknown:* the achievable
  per-iteration stub size and total compile time at 96×64 — if still too big, fall back to a small `rep` loop
  with a value-indexed jump for the address (trading some pointer cost back).
- **U1 — (was highest; now largely mitigated by Lever 0).** Compile-time-unrolled, packed, fixed-address
  writes make the framebuffer fill ~290K ops/frame with no pointers — *provided* U0 stays in check. Residual
  risk lives in U0, not here.
- **U2 — A single 32-bit multiply is ~10,000 ops; a divide ~68,000.** "A few FixedMuls/divides per column"
  silently = several × the frame budget. The hot path must be **mul/div-free** (reciprocal/scale LUTs +
  DDA incremental adds + `×constant`→shifts + 8.8/narrow math). *Unknown:* whether the raycaster's
  perspective/scale math fully reduces to LUT lookups + adds for *our* geometry, or leaves residual per-column
  mul/div we can't strength-reduce.
- **U3 — (was a pointer read; now mitigated).** The DDA map lookup is a **value-indexed jump into a baked-map
  switch-table** (Lever 0), not a ~1,000-op RAM pointer read. Residual cost is the jump-table dispatch + its
  contribution to compile size (folds into U0), and the DDA's variable step count (fps swing, G13).
- **U4 — 10M fj/s in CPython is unproven.** 2M→10M is 5×; the decoded-word cache + fast-mode may not get
  there in pure Python, forcing the C-extension/PyPy fallback (schedule + complexity risk). Everything above
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

**Net:** *flat, low-res, pointer-free (unrolled + packed), mul/div-free* DOOM is the realistic near-term
target, and it now hinges on **U0 (compile time) + U2 (mul/div elimination) + U4 (interpreter speed)** rather
than the old pointer-write wall. **Textured / HUD-text / higher-res are genuinely uncertain** — gated on both
a faster interpreter *and* compile-time headroom — promised only as flag-gated stretch goals. **R1's measured
ops/frame *and* assemble time are the joint go/no-go gate before any scaling.**

## Gaps, risks & scope cuts (resolved + tracked)

- **G1 — engine scope:** *resolved* → hand-written FlipJump, grid raycaster first, no c2fj.
- **G2 — blit cost:** *resolved* → `InMemoryScreen256` (device reads framebuffer from memory) ⇒ ~0 FJ ops.
- **G3/G15 — divides & fixed-point mul:** precomputed reciprocal/scale LUTs; fixed-point only (no FP);
  consider 8.8 precision where 16.16 is overkill; budget `FixedMul`/divide counts explicitly.
- **G4 — timing:** *resolved* → virtual frame-counter clock; no timer device.
- **G5 / point 3 — memory model:** segment-aware dense storage; compact hand-written address layout.
- **G6 — interpreter fast-mode:** strip stats/trace/breakpoints, hoist IO check; C-ext/PyPy fallback.
- **G7 — WAD asset pipeline:** R0 converter; per-surface flat-color LUT for flat mode.
- **G8/G23 — tiny-res features:** minimal tier = **3D view only**; auto-start a level (`-warp`-equivalent),
  **skip menu/HUD/text**; these return at higher tiers (per-tier table above).
- **G9 — audio:** out of scope (no audio device).
- **G11 — `@` grows with program size** (~25–30 at DOOM scale): keep hot data at low, `pad`-aligned
  addresses; treat the 1M budget as `@`-sensitive and re-measure.
- **G13 — variable frame cost:** floors/ceilings + sprites are scene-dependent → fps swings; set a worst-case
  scene target or accept variable fps.
- **G14 — BSP is pointer-heavy:** prefer the grid raycaster; real-DOOM BSP only if node counts stay low.
- **G16 — cache coherence with self-modifying code:** invalidate decoded-word cache on writes near `ip`.
- **G17 — device↔memory hook:** new interpreter API in MP-B.
- **G18 — framebuffer layout:** `SCREEN` is a packed byte buffer, device-readable directly.
- **G20 — assembler scalability:** zero-fill segments for big zeroed buffers; watch assemble time/memory.
- **G21 — tic/render decoupling:** allow rendering 1 of N tics so input/sim stay responsive.
- **G22 — flat-mode reference:** build a host-side reference model of our *exact* flat renderer to diff
  `SCREEN→PNG` against (stock DOOM only references textured mode).

## Open questions (still unresolved — to be settled by measurement or in R1)

These are genuinely undecided; most are *empirical gates*, not design debates:

- **OQ1 — Interpreter speed (U4):** does CPython + segment memory + decoded-word cache reach **10M fj/s**, or
  do we need the C-extension/PyPy fallback? Decided in **MP-A** by benchmark. Everything downstream assumes
  ~10M; at ~2M even flat 96×64 fails.
- **OQ2 — `w=32` vs `w=64`:** decided in **MP-A** by benchmark (speed + memory + pointer/`@` cost).
- **OQ3 — Lever-0 compile cost (U0):** the exact **shared-vs-inlined factoring** (per pixel / packed word /
  column) and whether the `fcall`-factored 96×64 build compiles fast enough to keep the TDD loop alive.
  Settled in **R1**; drives whether we keep full unrolling or a hybrid `rep`+value-jump.
- **OQ4 — Can the per-column math be made fully mul/div-free (U2)?** Whether the raycaster's
  perspective/scale/height math reduces entirely to integer-indexed LUTs + DDA adds + `×const`→shifts for our
  geometry, or leaves residual runtime `FixedMul`/`FixedDiv` we must budget. Settled in **R1**.
- **OQ5 — Precision: 16.16 vs 8.8 (vs mixed):** where can we drop to 8.8/narrow (≈4× cheaper mul) **without**
  visible angle/distance wobble? Per-quantity decision, validated against the reference model.
- **OQ6 — Decoded-word cache vs self-modifying `wflip` (G16):** the invalidation rule (writes near `ip`) is
  correctness-critical and its design is not yet fixed. Settled in **MP-A**.
- **OQ7 — Device↔memory hook API shape (G17):** exact interface for a device reading `SCREEN`/palette by
  address and writing the keyboard mailbox. Settled in **MP-B**.
- **OQ8 — Map/texture as baked switch-tables:** is the value-indexed jump-table for the tile map (and later
  textures) small enough to stay within the compile budget (ties to U0)? Map: likely yes; **textures: open**.
- **OQ9 — `fcall` non-reentrancy:** if any shared subroutine must call another shared subroutine, `fcall`'s
  single `ret_reg` can't nest — do we need a real `stl.call`/stack anywhere in the hot path (heavier)? TBD as
  the renderer's call graph takes shape in R1.
- **OQ10 — Variable frame cost / fps (G13):** accept variable fps, or target a worst-case scene and cap?
  Decided once R2 has real geometry.
- **OQ11 — Scope reach:** whether **textures, sprites, HUD/menu/text, and higher resolution** are achievable
  at all is gated on OQ1+OQ3 headroom — they remain flag-gated stretch goals, not commitments, until R1/R2
  numbers are in.

## Verification

- **TDD everywhere:** a unit test per FlipJump macro (FixedIO), per interpreter change, per device.
- **MP-A:** `prime_sieve.fj` time ÷ `op_counter` ⇒ fj/s ≥10M (else fallback); correctness unchanged;
  DOOM-scale **memory-footprint** test; w=32 vs w=64 comparison.
- **Render budget + compile budget:** track **both** `op_counter`/frame (≤1M for flat 96×64) **and assemble
  time + `.fjm` size** at every milestone. Micro-benchmark in isolation first: a Lever-0 unrolled packed
  fill (U0/U1) and a per-column-math path (U2) — confirm fixed-address unrolled writes are ~7@ (vs a
  pointer-based control), the hot path is mul/div-free, and the unrolled build compiles fast enough to keep
  the TDD loop alive — before trusting any tier promise.
- **Correctness:** `SCREEN→PNG` dump from memory, diffed against the host-side flat reference model (and a
  deterministic DOOM **demo** for textured mode); per-region op-count profiling.
- **End-to-end:** `fj --di keyboard --do InMemoryScreen256 doom.fjm` — scene renders, movement/fire keys
  register, measured fps meets the active tier; capture video as deliverable.
