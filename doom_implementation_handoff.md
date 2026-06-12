# DOOM-on-FlipJump — Implementation Handoff (post-1.5.0)

**This document is standalone — you need nothing else to drive the plan.** It supersedes the older
`doom_on_flipjump_plan.md` and fully absorbs it: every surviving lesson from that plan is inlined in
**Part II** (the cost models, Lever-0 mechanics, DOOM-pipeline reference, sim ladder, compositor
architecture, and the full risk/gap/open-question catalogs). The old file is now only a historical
record — you may archive or ignore it.

The re-baseline that motivated the rewrite: the old plan was written against a ~2–4M fj/s pure-Python
interpreter; flipjump 1.5.0's native engine re-baselined the world by ~50–80×, so its **budgets, tier
table, and "interpreter is the gate" framing are obsolete.** What survives is everything *not* about
interpreter speed — and, crucially, **the fj-op cost counts in Part II are still current** (they measure
macro-expansion cost in fj-ops, which the engine change did not touch; only the per-frame *budget* grew,
from ~1M to ~11.2M).

**How Part I and Part II relate (read this):** Part I is the **operating contract** — the staged process
(§4), the design-doc spec (§5), the D1–D15 decision backlog. Part II is **raw material**, not settled
design: it is the inherited knowledge the Stage-1 `DESIGN.md` *draws from and the owner re-decides*
through the Q&A. Nothing in Part II is a foregone conclusion; where it bears on a live decision it is
cross-referenced to a D-item. The U#/G#/S#/OQ# codes used as shorthand throughout Part I are all
**defined in Part II.**

**Where the work happens:** the game lives in the **`tomhea/doom-flipjump`** companion repo. The
`flipjump` package (`>=1.5.0`, `[io]` extra for the pygame device) is a pinned dependency — we do not
modify it except via its own finish-up handoff. Sessions working on the game need that repo in scope
(and the `flipjump-dev` skill installed, or clone `tomhea/skills` and read
`plugins/flipjump/skills/flipjump-dev/SKILL.md` + `reference/`).

---

# Part I — Operating contract (the plan & the process)

## 1. The world as it stands (inputs to this handoff)

### 1.1 flipjump 1.5.0 — what we build on

- **Native C engine (`_fjcore`)**: a **flat** loop (contiguous-array memory span) at **~100–300M fj/s
  stock, ~334M with PGO** (i7-12700H: ~280–307M compact, ~120–140M paged), and a **paged** loop for
  sparse/huge spans. Auto-used when present; abi3 wheels ship it for Linux/macOS/Windows (py3.10+).
  Pure-Python fast loop = fallback; the "featured" loop still owns `--trace`/breakpoints/`--profile`.
  Knobs: `--flat-max-words` / `FLIPJUMP_FLAT_MAX_WORDS` (default 2²³ words = 64MB array; 8B per
  span-word, ~0.1s/GB startup fill, **zero per-op cost** to raise), `FLIPJUMP_NO_NATIVE`,
  `FLIPJUMP_NO_FLAT`, and a **`storage_mode` report** (flat/paged) in `TerminationStatistics`.
  A **jump-target speculation tier is studied (GO: 5–9% miss, expected +50–80% → ~450–600M) but NOT
  built** — treat as headroom, never a dependency.
- **IO devices**: the model **collapsed to whole-device `--io MODE`** — **one device owns both keyboard
  and screen**; there is **no `--di`/`--do` input/output splitting and no two-device shared-window
  composition** (this replaces the old plan's separate `InMemoryScreen256` + `Keyboard` device pair).
  `standard` = terminal (default); **`pc`** = a pygame window with live keyboard + scaled 256-color
  screen (F11 fullscreen, window-close = clean stop). Headless/CI:
  **`PcIO.headless(events_file, frames_dir)`** replays scripted key events and writes PNG frames;
  **`InMemoryScreen`** captures the screen command stream. **All doom integration tests are headless
  replays against golden frames — no test needs a real window.** A **`DeviceMemory` hook** lets devices
  read/write program memory mid-run (the screen reads pixels straight from memory). Mode strings carry
  whitespace-separated factory params (`--io "pc <params>"`; none yet — so any future window option like
  scale/fullscreen-on-start arrives this way) — **no parallel config channel, ever**.
- **Screen commands**: `0x01` init, `0x02` set_palette, `0x03` update_screen (memory-hook read —
  **the primary present path, ~70 fj-ops/frame blit**), `0x04` update_rectangle (stride-correct —
  reserved for status-bar/menu rects; the 3D view redraws fully), `0x05` update_screen_raw (in-stream
  pixels — exists, but more expensive; **don't use it**).
- **Keyboard protocol**: non-blocking, tic-based. Poll = one `hex.input_hex` status (`0x0` none /
  `0x8` key-up / `0x9` key-down), then `hex.input` one keycode byte on events. Keycodes byte-sized:
  ASCII-like `<0x80`; arrows/shift/ctrl/alt at `0x80–0x86`. One poll = one tic = one game-loop
  iteration; the game keeps its own `keydown[]` flags (concurrency is state, not wire). **No timer
  device** — the frame counter is the clock. Scripted event files are the demo-playback / CI mechanism.
- **Debugger**: a real command REPL at breakpoints (raw + typed reads — `:b/:h/:B` vectors, array
  indexing, `:f:`/`:j:` op-words — step/skip/continue), documented in
  `flipjump/interpreter/debugging/README.md`.
- **Assembler**: 1.5–7× faster (Expr node-sharing + stl-prefix parse cache), bit-identical output;
  **hygienic macro/rep scoping** (user labels `d`/`i`/`n` are safe now; shadowing `w`/`dw`/`dbit` is a
  hard error). Known profile: macro-resolve and create-binary dominate, not parsing.
- **STL in flipjump**: all of 1.4.0 (`hex.scmp`, `min`/`max`, indexed/sequential pointer ops, decimal
  IO) **plus** `hex.mul_const`, `hex.abs`, `hex.fill_bytes`/`copy_bytes`,
  `hex.read_table`/`read_table_byte` (now the *fallback* table read, not the primary — see §3.2).
- **Catalog**: ~1,000 programs / ~2,400 tests exercise the toolchain — the regression backstop we
  inherit for free.

### 1.2 doom-flipjump repo — current state

- **PR #1** holds `fixed_point.fj` (`hex.fixed_mul`/`fixed_div`: 16.16 = n8/f4, 8.8 = n4/f2), the
  **LUT generator** (`lut_generator.py`), and their tests — relocated out of the flipjump repo.
  Independent of flipjump#354; mergeable once 1.5.0 is tagged. **It is the first execution item (§9),
  and it enters through a CR loop, into the directory structure WE design (§7), not the PR's paths.**
- The LUT generator currently emits **data tables**; it must learn to emit **dispatch-code tables**
  (§3.2) — that upgrade is part of the repo bootstrap.

### 1.3 flipjump finish-up tasks the game depends on (upstream, not game work)

A handful of flipjump-side "finish-up" work items sit *upstream* of the game on the dependency chain
(referenced as WI-* in §8/§10). They are not doom work — they live in the flipjump repo's own finish-up
handoff — but the game's start gates on the 1.5.0 tag that includes them, and two of them
(WI-E, WI-G2) directly affect game risks:

- **WI-E — assembler speedup (load-bearing for R-2).** Macro-resolve + create-binary dominate assemble
  time (not parsing); the column-unroll + mega-dispatch-table program is exactly WI-E's mega-LUT
  benchmark workload. If it underdelivers, the game leans harder on §3.1(a) (column buffer) over (b).
- **WI-F — jump-target speculation *measurement*.** Measures the studied speculation tier's feasibility
  (the engine itself is unbuilt — Part B). **The game treats any speculation win as headroom, never a
  dependency**; budget against today's ~280–334M.
- **WI-G — raw-frame present (`0x05`) + stride-correct `update_rectangle`.** Shipped; the game uses the
  memory-hook `update_screen` (`0x03`) as primary and may use `0x05` only as a purist fallback.
- **WI-G2 — flat-storage configurability + `storage_mode` observability (affects R-3).** Makes the flat
  limit configurable (`--flat-max-words` / env / API) and the flat-vs-paged mode reportable, so "the game
  runs flat" is *verifiable*, not assumed.
- **WI-H — the 1.5.0 release/tag** the game pins (`flipjump>=1.5.0`, abi3 wheels).

### 1.4 What the old plan got that 1.5.0 deleted (the delta table)

| Old plan (pre-1.5.0) | Now |
|---|---|
| Interpreter ~2–4M fj/s; MP-A speedup is *the* gate | **Done** — native engine ~280–334M; engine off the critical path |
| MP-B devices, MP-C debugger to be built | **Done** — `pc`/headless devices, memory hook, REPL debugger shipped |
| Budget: 1M fj-ops/frame (10M fj/s ÷ 10fps) | **~11.2M fj-ops/frame** (280M ÷ 25fps) |
| Target: 96×64 flat-shaded, 3D-view-only, 10fps | **160×100, textured, 256 colors, 25fps** |
| Grid raycaster (BSP "pointer-heavy, avoid") | **BSP walk now affordable** (~1.5–3M shared with column math + logic) — D1 |
| Lever-0 absolutism: zero runtime pointers anywhere | **Graded rules** (§3.4): static stores for pixels, dispatch-LUTs for tables, sequential streams OK |
| 4bpp knob to halve fill cost | **bpp=8 / 256-color baseline** (hex.vec fb at bpp=4 only for demos) |
| Map gridification + fidelity risk (U11) | **Likely moot if BSP is chosen** (real E1M1 geometry) — D1/D8 |
| `flipjump[screen]` extra (planned name) | **`flipjump[io]`** |
| Fixed-point STL ships in flipjump 1.5.0 | **Moved to doom-flipjump** (PR #1); flipjump keeps abs/byte-buffers/read_table |

Still standing (all inlined in Part II): the per-op cost intuitions (pointer ≫ fixed-address; div ≫ mul;
variable shifts are pointer-class — U6; full table in §A), the Lever-0 unrolling mechanics (§B), the
DOOM rendering pipeline (§C), the S0–S2 sim ladder (§D), the compositor/pass architecture + `blit_rect`
(§E), the TDD methodology + `hex.scmp` rule (§3.5), the reference-model verification idea (§H),
`@`-sensitivity (U7), and 32×32→64 fixed-point intermediates (U5) — the latter two in the risk catalog
(§F).

---

## 2. Re-baselined target & frame budget

**Primary target (owner decision): 160×100, textured, 256 colors, 25fps.**
**Stretch:** 320×200@25 textured — *only after* the speculation tier lands (~450–600M).
**Fallbacks:** 160×100 flat-shaded, or 160×100 textured @12.5fps.

Budget at 25fps on the measured engine: **~11.2M fj-ops/frame.** Estimated spend with the mandatory
techniques (§3):

| Per-frame cost | With mandatory techniques |
|---|---|
| Pixel stores (16K px × ~80 ops, static) | ~1.3M |
| Texture + colormap reads (16K × ~100–200 ops, dispatch-LUT) | ~1.6–3.2M |
| Column math (160 cols) + BSP walk + game logic | ~1.5–3M |
| Present (`update_screen` via memory hook) + input poll | ~negligible (~70 + ~tens) |
| **Total** | **~5–7M of 11.2M — fits with ~2× margin** |

The anti-budget that makes §3 mandatory: per-pixel **pointer** writes are ~500 ops each at w=32 →
16K × 500 = **8M/frame on stores alone**; `read_table` pointer reads per pixel are similar. Neither
fits. **w=32 confirmed** (16.16 fits one word exactly).

---

## 3. Mandatory techniques (every design in the document respects these)

### 3.1 Static pixel stores — the store side of the pixel path is compile-time-addressed

Two candidate designs; **decide in R1 with measurements** (this is the design doc's biggest open
decision, D2):
- **(a) Fixed column buffer:** render into a fixed-address `hex.vec` column (all per-pixel math on
  static addresses), then one *sequential* pointer pass per column places it in the framebuffer.
- **(b) Full column unroll:** `rep(SCREEN_WIDTH, x) render_column x` — framebuffer addresses become
  compile-time constants; **zero pointer derefs on the entire pixel path**; costs WIDTH copies of the
  column code (program size + assemble time — §3.3, §10 R-2).

**Owner's decision criterion:** if pixel-color computation can run statically on fixed
compile-time-known addresses, **hex-memory wins; otherwise packed-byte.** Owner leaning: **use
hex-memory for pixels.** Note the interplay to resolve in the design doc (D3): the screen device's
primary read is a packed-byte framebuffer via the memory hook; hex.vec framebuffers are stated to work
at bpp=4 (demos). The framebuffer cell encoding (packed-byte vs hex/register-form vs 2-hex-per-pixel)
must be settled *jointly* with the store design and the device's read format — flagged for the
contradiction hunt (§6).

The read side (texture sampling) is runtime-indexed regardless — static stores fix only stores;
dispatch-LUTs (§3.2) fix reads.

### 3.2 Dispatch-LUTs for every table access — "fixed-address-LUT everything you can" (owner directive)

*(a.k.a. the "hex.and / dispatch-table technique" — the `hex.xor`-jumper idiom below, per `tables_init.fj`)*

NOT `hex.read_table` pointer reads (those are the fallback). Mechanism (study
`flipjump/stl/hex/tables_init.fj`):
- the table is a **pad-aligned CODE region** — entry *k* at `base + k·dw`, base low bits zero;
- a hex variable's data bits sit exactly at the jump-word bit positions encoding `k·dw`, so
  `hex.xor jumper, index_nibble` **IS** the base+index computation (a few `@`);
- `wflip ret+w, return, jumper` jumps through it into entry *k*;
- entries are single ops ("stride 1") jumping to where the entry is really handled: the **hypercube
  chain** (entry *d* flips one result bit, jumps to `d ^ (1<<(#d−1))`, cascades to entry 0 → ret;
  avg `log(n)/2` ops) or **per-entry handler code**;
- cost: **~10@ per lookup — 10–30× cheaper than `read_table`**;
- multi-nibble results (32-bit `finesine` entries): evaluate **one aligned table per result-nibble**
  (8 cheap dispatches) vs **per-entry handlers** (1 dispatch + ~popcount flips) — per-table decision
  (D4).

Apply to: `finesine`/`finecosine`, reciprocal/scale, `yslope`, `viewangletox`/`xtoviewangle`,
colormaps — and **evaluate texture data itself as dispatch tables** (D5). Keep indices nibble-aligned
(U6 holds: no runtime-amount shifts to form an index). **Every generated table gets its own thorough
test**: generated `.fj` program + host-reference fixtures over many indices, including first/last
entries and wrap boundaries.

### 3.3 Memory-layout rules — the game MUST run on the flat path

- **Verify, don't assume**: assert `storage_mode == flat` in the test harness.
- Keep total address span under the flat limit, or raise it explicitly (`--flat-max-words` /
  `FLIPJUMP_FLAT_MAX_WORDS`); cost is RAM (**8B × span**, whole span touched at startup) + fill time
  (~0.1s/GB) only — **zero per-op speed cost**. Concretely: a 2²⁶-word program ≈ **512MB** flat (and a
  future per-op prediction array could roughly double that). Document the knob and the paged-mode
  fallback in the doom README.
- **Aligned dispatch tables pad to power-of-two boundaries and inflate the span** — lay tables out
  deliberately, **largest alignment first**, and keep a *span ledger* (a living table in the design
  doc, like the ops ledger) so padding waste is summed, not discovered.
- Compute state/angles in **fixed-address `hex.vec` registers**; pointers only for genuinely
  runtime-indexed *streams* (map/seg data), walked sequentially.

### 3.4 Graded access-cost rules (replaces the old plan's zero-pointer absolutism)

| Access pattern | Use | Never |
|---|---|---|
| Program state, per-column scratch | fixed-address registers | — |
| Framebuffer stores | static stores (§3.1) | per-pixel pointer writes |
| Table reads (trig/LUT/colormap/texture) | dispatch-LUTs (§3.2) | per-pixel `read_table`/`*_nth_*` |
| Sequential data streams (map walk, column blit in design (a)) | `*_and_inc` sequential ops (O(1)/step) | `*_nth_*` per element (O(w) twice per call) |
| Rare random access (setup, per-level init) | `read_table`/`ptr_index` fallback | unrolling them (O(w) size × count) |

### 3.5 Methodology carried over unchanged

TDD per macro (test first, sterile environment, boundary inputs per behavior path); **`hex.scmp` for
anything that can be negative** (the catalog's #1 latent-bug class); `--werror` always;
byte-exact verification via `flipjump.assemble_and_run_test_output(...)`; headless golden frames for
anything visual; assemble time + `.fjm` size + ops/frame are tracked metrics from the first renderer
experiment. The 1.5.0 assembler's hygienic scoping removes the old `i`/`d`/`n` naming landmine.

---

## 4. The process contract (stages and gates)

Each stage ends with **explicit owner approval**; nothing from a later stage starts early. **No game
code is written before the final design document exists** (Stage 2 complete).

1. **Stage 1 — Design Document.** Write `DESIGN.md` (in the doom-flipjump repo's git, so diffs are
   reviewable) covering *every* component per the spec in §5. Built iteratively through owner Q&A —
   expect many rounds; batch questions; record every decision in the doc itself (a Decisions section
   with IDs, not chat history).
2. **Stage 2 — Contradiction hunt.** Adversarial pass over the agreed document per the checklist in
   §6; fix every contradiction *in the document*; re-approve. Output: **the final document.**
3. **Stage 3 — Directory tree.** Propose the full project structure (§7); agree on it.
4. **Stage 4 — Iterative stage cutting.** Slice the plan into small, independently testable execution
   stages (§8); agree on the slicing.
5. **Stage 5 — Execution.** First item, before anything else: **CR-loop the files of
   doom-flipjump PR #1 until approved, relocating them into the Stage-3 tree** (§9). Only then execute
   the stages in order.

---

## 5. Stage 1 — the Design Document spec

**Form:** one markdown file, `DESIGN.md`, target ≤~1000 lines (soft), committed early and evolved in
git. Big yet concise: every idea, every execution component, every fj-component, every generating
script — in text we both agree on.

**Required global sections:**
- **Targets & budgets** — §2's tables as *living ledgers*: the ops/frame ledger (every component adds
  its line; the sum must stay under 11.2M with stated margin) and the **address-span ledger** (every
  table/segment with its alignment padding; sum vs the flat limit).
- **Decisions** — every D-item below, with its resolution, rationale, and what measurement (if any)
  settled it. Unresolved = listed as open with the experiment that will decide it.
- **Memory map** — the segment/alignment layout, largest-first (§3.3).
- **Testing strategy** — the per-layer test pyramid (host unit tests → per-macro fj tests →
  per-table generated tests → golden-frame renderer tests → headless scripted-replay E2E).
- **Glossary & conventions** — units (fj-ops vs `@`), nibble/byte/hex terminology, naming.

**Per-component template** (every component gets all fields, however brief):
1. **Purpose** — one paragraph, what it does.
2. **Supplies** — its interface: macros/functions/files it exports, with signatures.
3. **Depends on / related to** — which components it consumes; who consumes it.
4. **Assumes** — preconditions: init requirements, value ranges, alignment, encoding (16.16/8.8),
   "callers guarantee X".
5. **Data & layout** — what it stores, where (fixed registers / table region / stream), span impact.
6. **Time complexity** — ops/call and ops/frame against its budget-ledger line.
7. **Space complexity** — program-size ops + address-span (incl. alignment padding) + assemble-time
   impact.
8. **Testing** — concrete test ideas: fixtures, boundary cases, golden data, host-reference diff.
9. **Open questions / fallbacks** — what's still uncertain and the plan B.

**Component inventory to cover** (seed list — the document may split/merge, but nothing here may be
silently dropped):

*Host-side (Python, doom-flipjump repo):*
- **WAD parser/extractor** — levels (VERTEXES/LINEDEFS/SIDEDEFS/SECTORS/SEGS/SSECTORS/NODES/THINGS)
  + assets (PLAYPAL, COLORMAP, textures/patches, flats, sprites) per the D7 scope.
- **LUT generator** (from PR #1, upgraded) — emits **dispatch-code tables** (§3.2) *and* data tables;
  per-table emit modes (hypercube chain / per-entry handlers / per-result-nibble); alignment-aware.
- **Map compiler** — WAD level → baked `.fj` structures (BSP nodes/segs or grid, per D1).
- **Texture/colormap compiler** — D5's output format.
- **Reference model** — host-side golden implementation of *our exact* renderer + sim for frame/state
  diffing.
- **Build system** — assemble pipeline (w=32, `--flat-max-words`, `--werror`), Makefile/script, CI.
- **Test harness** — headless replays (`PcIO.headless`), golden-frame compare, per-table test runner,
  ops-budget profiler (per-region op counts via `--profile`/featured loop on small builds).

*FJ-side (the game program):*
- **Memory map / layout module** — the address plan itself (a component: it has invariants and tests).
- **Fixed-point math layer** — `fixed_point.fj` (PR #1): fixed_mul/fixed_div 16.16 + 8.8, plus
  whatever D6/D13 add (intermediate-width handling).
- **LUT access layer** — the dispatch-jumper idioms, one per table family.
- **Framebuffer + pixel-store layer** — D2/D3's resolved design.
- **Renderer** — visibility (D1: BSP walk vs raycaster), wall column renderer, floor/ceiling
  spans/visplanes, sprite renderer (D7), lighting/colormap application point (D11).
- **Game loop & tic** — input poll + `keydown[]`, player move/collide, doors/specials, entities/AI
  (scope ladder S0–S2, §D, re-budgeted), combat, level transitions.
- **Present layer** — init/set_palette/update_screen; `update_rectangle` only for status-bar/menus.
- **HUD/status bar/menu/text passes** — now plausible at 160×100 (D7); compositor/pass pipeline and
  the `blit_rect`/glyph design are in §E.
- **Debug/diagnostics** — op-count probes, frame dumps, on-screen debug values (cheap at this budget).

**Decision backlog** (the Q&A agenda; owner leanings recorded where known):
- **D1 — Visibility:** BSP walk (real DOOM maps, now affordable) vs grid raycaster (simpler; the
  pre-1.5.0 default — §C). Settles map compiler + U11's fate.
- **D2 — Static-store design:** column buffer (a) vs full unroll (b); decided by R1 measurements
  (ops AND assemble time AND size). The unroll feeds the assembler mega-program workload.
- **D3 — Framebuffer encoding:** hex-memory (owner leaning) vs packed-byte; must co-resolve with the
  device's read format and bpp=8 baseline. *Known tension — see §6.*
- **D4 — Per-table dispatch shape:** per-result-nibble tables vs per-entry handlers, per table.
- **D5 — Texture storage:** dispatch tables vs sequential streams; texture count/resolution vs the
  span ledger.
- **D6 — Precision per quantity:** 16.16 vs 8.8 (≈4× cheaper mul) per variable, validated against the
  reference model (wobble risk).
- **D7 — Feature scope at 160×100:** sprites/enemies (S2?), HUD/status bar, menus, text, demo
  playback; what ships in the first playable vs flag-gated later.
- **D8 — Maps:** which level(s); full E1M1? entity counts; shareware `doom1.wad` for dev, Freedoom
  for anything redistributed (CI fixtures).
- **D9 — Frame pacing:** strict 25fps vs uncapped; tic:render ratio (1:1?).
- **D10 — Memory map:** the concrete largest-alignment-first layout + span budget.
- **D11 — Colormap/lighting application point:** per-pixel (in the texture-read 100–200 op estimate)
  vs per-column; light diminishing fidelity.
- **D12 — Test granularity:** what gets unit-tested vs golden-framed; how many golden frames; demo
  scripts.
- **D13 — Fixed-point intermediates:** 32×32→64 product handling at w=32 (U5).
- **D14 — Directory tree** (Stage 3 formalizes).
- **D15 — Anything PR #1's CR surfaces** (API/naming/test-style changes to fixed_point + generator).

---

## 6. Stage 2 — contradiction-hunt checklist

Run these *mechanically* over the agreed document; every hit gets fixed in-doc before approval:

- **Ledger sums:** per-component ops/frame lines sum under 11.2M with the stated margin; span lines
  (incl. alignment padding) sum under the chosen `--flat-max-words`.
- **Assumes ↔ supplies:** every "assumes" in any component is some other component's explicit
  "supplies" (init order included — who runs `hex.init`/`stl.ptr_init`/table inits, and when).
- **Encoding coherence:** framebuffer cell encoding (D3) = what the pixel-store layer writes = what
  the screen device reads via the hook = the bpp the palette/init declares. *(Known tension already:
  owner leaning hex-memory pixels vs packed-byte primary device read at bpp=8 — must resolve here if
  not before.)*
- **Index discipline:** every dispatch-LUT index is produced nibble-aligned without runtime shifts
  (U6); every table's index width matches its alignment/padding entry in the span ledger.
- **Units:** no `@`-counted figure compared against a raw-ops figure without conversion; w=32
  consistently assumed everywhere (no leftover w=64 costs from the old plan).
- **Call discipline:** `fcall`/`ret_reg` nesting depth per call chain ≤ declared registers; recursion
  only where a stack is declared.
- **Pacing math:** tic rate × per-tic costs vs fps × per-frame costs consistent with D9.
- **Decision propagation:** every D-resolution is reflected in *all* components that referenced it
  (grep the D-id).
- **Fallback reachability:** each fallback (flat→paged, textured→flat, 25→12.5fps, unroll→column
  buffer) is actually reachable from the designed structure without rewriting other components.

---

## 7. Stage 3 — directory tree

Propose the full doom-flipjump structure: fj sources (engine layers per §5's inventory), generated
output (tables/maps — generated files' in-repo vs build-dir policy decided here), host tools, tests
(unit/golden/replay fixtures), docs (`DESIGN.md`, README), CI. PR #1's files get their *designed*
homes here — explicitly mapping PR-path → designed-path. Owner approves the tree before Stage 4.

---

## 8. Stage 4 — iterative stage cutting

Rules: each stage is small, independently runnable, tested, and ends with something demonstrable
(a passing suite, a rendered frame, a measured number); each states its exit criterion up front;
measurement stages (D2's R1 experiment) come before the designs they decide. Expected shape (sketch,
non-binding — Stage 4 formalizes it):

```
flipjump 1.5.0 tagged (incl. finish-up §1.3: WI-E assembler, WI-F speculation
                       measurement, WI-G raw-frame, WI-G2 flat config, WI-H release)
        │
        ▼
S5.0  PR #1 CR-loop → fixed_point + LUT generator land in the designed tree
S5.1  LUT generator learns dispatch-code emission + per-table tests
S5.2  R0: WAD pipeline + generated tables (trig/reciprocal/yslope/colormaps)
S5.3  R1: renderer vertical slice — settle D2 (static-store design) with
      measured ops/frame + assemble time + size; settle D3 jointly
S5.4  R2: full renderer at 160×100 textured + S0 walk/collide sim
S5.5  R3: doors/combat/entities/HUD per D7; polish to 25fps
      (320×200 stretch only after the speculation tier exists)
```

---

## 9. Stage 5 kickoff — PR #1 CR-loop (the first execution item)

Target: https://github.com/tomhea/doom-flipjump/pull/1 (`fixed_point.fj`, LUT generator, tests).

Procedure:
1. Review every file against the final design doc's standards: `hex.scmp` rule, `--werror`-clean,
   complexity doc-comments (`Time`/`Space`, `@requires`, `@Assumes`, `@output-param`), test style
   (byte-exact, boundary inputs per path, catalog-style registration), naming conventions, and the
   D15 API decisions.
2. Post findings as a CR; iterate with the owner until **approved** — a real loop, not one pass.
3. Relocate the files into the Stage-3 tree (**not** the PR's stated paths) as part of the merge.
4. Merge gates: flipjump 1.5.0 is tagged (the PR is independent of flipjump#354 but builds on the
   released package); all PR tests green in the doom-flipjump CI.

Only after this lands does S5.1+ execution begin.

---

## 10. Risks (re-baselined)

- **R-1 — Budget estimates are projections.** The §2 ledger (~80 ops/px store, ~100–200 ops/px
  texture read) is estimated, not measured; S5.3 measures before R2 commits. Margin is ~2× — real,
  but not infinite. Fallbacks: flat-shaded / 12.5fps.
- **R-2 — Assembler scalability is now load-bearing** (upgraded from the old plan's informational
  R-C). Column-unroll + mega dispatch tables make the program large (hundreds of KB of ops → a few
  MB); the build leans on the 1.5.0 assembler speedup (whose mega-LUT benchmark *is* the game-shaped
  workload) and on flipjump finish-up WI-E. If assemble time still kills the TDD loop, design (a)
  (column buffer) is the relief valve.
- **R-3 — Span vs flat path.** Power-of-two table padding can balloon the address span past the flat
  limit silently → paged mode → ~2.5× slowdown. The span ledger + `storage_mode` assertion are the
  guards.
- **R-4 — D3 encoding tension** (hex-memory pixels vs packed-byte device read) is a real
  contradiction candidate that touches device, store layer, and present path — resolve early, in the
  design doc, not in code.
- **R-5 — flipjump finish-up timing.** S5.0 gates on the 1.5.0 tag; WI-E/WI-G2 affect R-2/R-3. Track
  the finish-up handoff's status at session start.
- **R-6 — Fidelity unknowns carried over:** 8.8 wobble (D6), 32×32→64 intermediates (U5), `@` growth
  with program size (U7) — all old-plan risks that survive re-baselining, now with much more headroom.

---

## 11. Session bootstrap (for whoever picks this up)

1. Repo scope: get `tomhea/doom-flipjump` added to the session (game work happens there;
   `tomhea/flip-jump` is reference/dependency only).
2. `pip install "flipjump[io]>=1.5.0"` (abi3 wheels include the native engine); verify with a
   benchmark run + `storage_mode` report.
3. flipjump-dev skill: `/plugin marketplace add tomhea/skills` + `/plugin install flipjump@tomhe`;
   in remote sessions without the plugin, clone `tomhea/skills` and read the skill files directly.
4. Read, in order: Part I of this handoff → Part II (inherited cost models, mechanics, risks) →
   `flipjump/stl/hex/tables_init.fj` (the dispatch-LUT mechanism) → PR #1's diff.
5. Start Stage 1: open `DESIGN.md` in doom-flipjump, seed it from §5, and begin the Q&A.

---

# Part II — Inherited design knowledge (raw material for Stage 1)

**Status of this Part:** this is the consolidated knowledge from the pre-1.5.0 plan, *re-baselined and
inlined so the handoff is standalone.* **It is input to the design, not the design.** The Stage-1
`DESIGN.md` adopts, revises, or rejects each item through the owner Q&A; where an item bears on a live
choice it points to a D-decision. **Read the fj-op cost numbers (§A) as current** (they count macro
expansion, unaffected by the engine change); read every *budget/fps/resolution* number as superseded by
Part I.

## §A — Per-op cost model (fj-op counts; still current at w=32)

These are the foundation costs every design is measured against. Pre-1.5.0 estimates at `@≈27`, w=32 —
**still valid as fj-op counts**; only the per-frame budget changed (1M → ~11.2M).

| Operation | stl macro | cost (w=32) | ≈ fj-ops |
|-----------|-----------|-------------|----------|
| Fixed-address byte write | `hex.xor`-style | ~7@ | ~190 |
| **Pointer** byte write (runtime addr) | `write_byte` | 41@+197 | **~1,300** |
| **Pointer** byte read (runtime addr) | `read_byte` | ~33@ | **~1,000** |
| **Indexed** read/write (`read_nth_*`) | `ptr_index`+… | O(w) ops **and** O(w) *space* per call site | pointer-class |
| 32-bit multiply (8 hex) | `hex.mul` | ~350@+1300 | **~10,000** |
| 16-bit multiply (8.8, 4 hex) | `hex.mul` | ~88@+320 | **~2,700** |
| 32-bit **divide** (8 hex) | `hex.div` | ~2300@+6400 | **~68,000** |
| constant shift (`<<`/`>>` by const) | `hex.shl_hex` | ~8@+32 | **~250** |

**Estimate reconciliation (a contradiction-hunt item, §6):** Part I §2 uses ~80 ops for a *static packed*
pixel store (multiple px/word, fixed address — cheaper than the ~190 single-byte fixed write) and quotes
a per-pixel *pointer* write at "~500 ops"; this table says ~1,300. The two differ because of packing and
estimate vintage. The conclusion is invariant under either figure — **per-pixel pointer writes are
unaffordable** (16K × 500–1,300 = 8–21M/frame on stores alone) — but **S5.3 must measure the real number**
before R2 commits.

**Three lessons that drive the whole renderer:**
1. *The framebuffer-write reckoning.* A pixel at runtime `(x,y)` is a runtime address → naïvely a pointer
   write. That's why **static stores (§3.1) are mandatory**, not an optimization.
2. *Per-column math is the second threat.* One 32-bit `FixedMul` ≈ 10K ops, one `FixedDiv` ≈ 68K. Even one
   divide per column × 160 ≈ 11M — the entire frame. ⇒ **the hot path must be mul/div-free:**
   reciprocal/scale/yslope LUTs (replace *every* divide), `×const`→shifts+adds (cost scales with the
   operand's on-bits, so sparse constants are cheap), 8.8/narrow widths where precision allows, LUT-multiply
   for bounded operands, and DDA incremental adds over per-step muls.
3. *Never `rep`-unroll an O(w)-sized macro.* Measured in the catalog campaign: **243 unrolled indexed
   (`read_nth_*`) reads ≈ 7-minute assemble**; ~729 of them ran ~12s interpreted. Only **O(1)-size bodies**
   (fixed-address ops, a few flips, an `fcall`) may be unrolled — the quantitative backing for R-2 and the
   design rule behind every Lever-0 stub.

## §B — Lever 0: pointer-free by compile-time unrolling (the "constant algorithm")

The mechanism behind §3.1(b) (full column unroll) and the dispatch-LUT idiom (§3.2). A runtime pointer is
only needed when an address is computed *at runtime*; unroll with `rep(n,i)` and **every address becomes a
compile-time constant** → fixed-address write/read (~7@), never a ~1,300-op pointer op. Cost moves from
*time* into *space* (assembled size).

```
rep(W*H/PXPERWORD, i) calc_pixel SCREEN + i*WORD_STRIDE   // each i is constant ⇒ fixed address
```

- **Writes → fixed-address, packed.** Unrolling + packed word writes ⇒ a full fill in fixed-address
  word-writes (~7@), zero pointers. Replaces the fragile "sequential-pointer-write primitive" idea.
- **Branchless per-pixel selection.** Wall height per column is runtime, but don't branch on it: unroll all
  `H` rows and have the tiny `calc_pixel` **select** ceiling/wall/floor color by comparing its
  *compile-time* `y` against the column's runtime top/bottom held in **fixed-address per-column scratch**.
  Same work every pixel, data-selected — the "constant algorithm." `calc_pixel` must be *tens* of ops.
- **The calling-convention crux (settled in R1 / D2).** A *shared* `fcall` body cannot contain a
  per-call-site constant, so each unrolled stub must (a) write its compile-time `y` (or pre-staged per-row
  data) into a single fixed `CUR_Y` scratch — a constant-into-fixed-address write, a few ops; (b) `fcall`
  the shared compare/select/pack body; (c) write the result to its constant `SCREEN+i*STRIDE` address.
  Whether the compare lives in the stub (bigger stub, no `CUR_Y` write) or the body (smaller stub, one
  extra write) is R1's first experiment.
- **Share per-column, unroll per-pixel.** Unroll geometry once per column (`rep(W,x)`, 160×) and only the
  cheap fill per pixel — so the ~10K-op math happens ~160×, never 16K×. Each column's setup copies that
  column's top/bottom/color into the single fixed scratch the shared body reads.
- **Factor with `stl.fcall`/`stl.fret`, don't inline.** A heavy `calc_pixel` inlined 16K× = 16K×(its size).
  Put the heavy, **address-independent** logic (LUT lookups, color select, packing) in **one shared leaf
  subroutine** via `stl.fcall` (≈`@-1` time, ~0 space) / `stl.fret` (≈1) — far cheaper than `stl.call`'s
  ~2.5w@ stack call, and fine because the body is a leaf (no recursion ⇒ no stack; one `ret_reg` per nesting
  level if bodies ever chain — OQ9). Only a tiny address-specific stub inlines per iteration.
- **Constant-table reads by runtime value → jump-table, not pointer.** This is exactly the dispatch-LUT of
  §3.2: set a hex to the value, jump into `rep`-generated code at that offset (the `stl/hex/mul.fj`
  `switch:`/`dst: ;switch` idiom). Reads constant data by value with **no RAM pointer**.
- **The price is space-complexity** — now load-bearing (R-2). Unrolling a fat macro explodes assembled size
  *and* compile time. Mitigations: factor (above), unroll over **packed words** not pixels, share geometry
  per-column, bound table sizes, and **track assemble time + `.fjm` size as first-class metrics from R1.**
  Fallback ladder if it overruns: shrink the stub → fewer px/word → smaller res → hybrid (unroll rows, loop
  columns) → §3.1(a) column buffer → indexed loop for non-hot surfaces only.

## §C — DOOM rendering pipeline (the reference the renderer reimplements)

Player state: position `(x,y)`, eye height `z`, view angle. Per frame:
1. **Visibility (D1).** Stock DOOM walks a precompiled **BSP tree** front-to-back, no overdraw, clipping
   screen columns as it goes (~55@/node — pointer-heavy, the reason the pre-1.5.0 plan chose a **grid
   raycaster** instead: one ray per column into a tile map, no pointer chasing). *Re-baseline:* BSP is now
   affordable (D1) — both options are live.
2. **Walls (`R_DrawColumn`).** Per visible column: distance → on-screen wall height via a **divide**
   (`scale ≈ projection / distance`), then draw a vertical strip. Textured = sample a texture column with a
   fixed step (`frac += step`; `texel = source[frac>>FRACBITS]`); flat = one shade.
3. **Floors/ceilings (`R_DrawSpan`).** Horizontal spans, perspective-correct via a per-row distance
   (`yslope[]`) and a divide per span. Flat = solid color (cheap). DOOM batches these as *visplanes*.
4. **Sprites/things.** Masked (transparent) columns, distance-sorted, clipped against walls.
5. **Lighting.** A **colormap** LUT chosen by distance + sector light darkens texels (D11: apply per-column
   in flat mode, not per pixel — naïve per-pixel colormap is a pointer read per pixel, ~6M+/frame).

All 16.16 fixed-point, leaning on precomputed tables: `finesine/finecosine`, `finetangent`,
`viewangletox`/`xtoviewangle`, `yslope`, `distscale`, `colormaps`. **Takeaway:** almost everything reduces
to table lookups + a few fixed-point mul/div per column/span → exactly what dispatch-LUTs (§3.2) make
cheap.

## §D — Game simulation ladder (the other half — rendering alone is a screensaver)

Sim shares the frame budget; on a tile/seg map it is tile lookups + signed compares + adds — the cheap op
class — so it is budgeted, not feared. Compile-time-gated like render passes:
- **S0 — Walk (ships with R2/minimal).** Poll → turn (angle ± const, table-wrapped) → strafe/forward
  (pos += dir·speed, `×const`→shifts) → **collision**: check the 1–2 destination tiles (value-indexed map
  lookup, same idiom as the DDA) with axis-separated slide (DOOM-feel wall glide, not full `P_TryMove`).
  ≈ a few K ops/tic.
- **S1 — Doors + hitscan (minimal, after S0).** Use-key opens a door tile (timed state in a small
  fixed-address door table); fire = one extra DDA ray (same code as a render column) → hit wall/entity.
  ≈ one column's ops/shot.
- **S2 — Entities (very-good tier, with sprites).** N **fixed entity slots** (pos, type, hp, state) at
  fixed addresses — **no DOOM thinker lists** (pointer-chasing). Per tic per entity: line-of-sight ray
  (DDA), chase step (tile-collision like the player), melee/hitscan damage. Cap actives per tic
  (round-robin) to bound the frame.
- Health/armor/ammo = fixed-address counters touched by S1/S2. Level exit = a tile/linedef value. Game
  over/restart = re-init the state block. **No** savegames, demos-from-WAD, multiplayer, or audio.

**Signed-value warning (the catalog's #1 latent-bug class):** deltas, velocities, angle wraps, `mid-1`
underflows are all signed → compare with **`hex.scmp`, never `hex.cmp`** (three real catalog bugs shipped
past green fixtures from exactly this). A review-checklist item on every `.fj` PR.

## §E — Compositor / extensibility architecture (build for HUD/text without rework)

Mirror DOOM's `colfunc`/`spanfunc` indirection: one `draw_column`/`draw_span` macro whose body is chosen
by the `TEXTURED` flag. Only the innermost loop + per-column data differ; everything upstream is identical.
- **Pass pipeline.** `render_frame` is a fixed, ordered list of compile-time-gated **passes** into one
  `SCREEN`: `render_3d_view → [render_sprites] → [render_statusbar] → [render_text/messages] →
  [render_menu]`. Minimal compiles only the first; later tiers flip flags. Adding a pass = one gated call,
  never touching the 3D core.
- **Sub-window draw target.** The 3D view writes a `(VIEW_X,VIEW_Y,VIEW_W,VIEW_H)` rect inside `SCREEN`;
  overlay passes own the rest — HUD/text claim rows the 3D view doesn't, no coordinate retrofit. (At
  160×100 these overlays are now plausible — D7.)
- **Reusable blitter.** `blit_rect(src_addr, dst_x, dst_y, w, h, [transparent_idx])` is the shared backbone
  for sprites **and** HUD graphics **and** glyphs — write once (R3), the rest are callers.
- **Text = glyph LUT + blitter.** `draw_string(x, y, ptr_to_chars)` walks bytes, indexes a font glyph table
  (fixed-address LUT, DOOM's `hu_font`), calls `blit_rect` per glyph. Menus/stats = `draw_string`+
  `blit_rect`. **Stub the API now** (flag-gated), so the seams exist from day one.
- **Cost note.** Overlay passes are framebuffer-write-heavy like walls — enabled only at tiers whose budget
  pays for them. *Architecture free, pixels not.*
- **Present-path caveat (1.5.0):** the 3D view redraws fully every frame; `update_rectangle` (cmd `0x04`)
  is reserved for status-bar/menu rects that *don't* redraw — not the main view.

## §F — Risk catalog (U-codes; re-baselined)

Part I's live risks are R-1…R-6; these are the detailed inherited risks they build on. "(now: …)" marks
the re-baseline.

- **U0 — Compile time / assembled size is the engineering #1.** Unrolling + mega switch-tables can make the
  *assembler* take minutes and balloon the `.fjm` (243 unrolled O(w) reads ≈ 7-min assemble). *(now: R-2;
  mitigated by §B factoring + the 1.5–7× faster 1.5.0 assembler, whose mega-LUT benchmark is this exact
  workload.)*
- **U1 — Framebuffer write wall.** *(now: mitigated by static stores §3.1; residual risk lives in U0/R-2.)*
- **U2 — Mul ≈ 10K ops, div ≈ 68K.** "A few per column" = several× budget. Hot path must be mul/div-free
  (§A lesson 2). *Unknown:* whether our geometry fully reduces to LUTs + adds (D-OQ4 below).
- **U3 — Map lookup.** *(now: a value-indexed dispatch jump §3.2, not a ~1,000-op pointer read.)*
- **U4 — Interpreter speed.** *(now: resolved — native engine; off the critical path.)*
- **U5 — 32×32→64 fixed-point intermediates.** A 16.16 `FixedMul` needs a 64-bit product (two words at
  w=32) with hand-carried overflow — extra ops + macro complexity; pushes toward 8.8 where precision
  allows. (D13.)
- **U6 — Variable shifts are pointer-class.** Constant shifts (`frac>>FRACBITS`) are ~free; any
  **runtime-amount** shift is expensive. **LUT indices must be formed without a runtime shift** (e.g.
  integer-distance index), or they quietly cost like pointers.
- **U7 — `@` grows with total program size.** Every per-op cost scales with `@≈27`; a DOOM-scale program
  (huge LUTs + textures + map) can push `@` higher, inflating *all* costs uniformly. Big static tables
  aren't free even when fixed-address.
- **U8 — Assemble time/memory at DOOM scale.** Mega-tables may make the assembler slow/memory-hungry.
  *(now: folds into R-2; the LUT generator + zero-fill segments must not regress assemble time.)*
- **U9 — Divide ≥ multiply, and lighting can secretly go per-pixel.** Replace *every* runtime divide with a
  reciprocal/scale LUT. Apply the colormap **once per column** in flat mode (D11), never per pixel.
- **U10 — Framebuffer clear.** A full per-frame clear = another ~W·H writes. The raycaster avoids it
  *only because every column is drawn ceiling→wall→floor with no gaps* — "no clear" is a **load-bearing
  invariant.** Any partial-draw path (sprite gaps, letterbox) reintroduces a clear and needs explicit fills.
- **U11 — Gridification fidelity.** Rasterizing E1M1 linedefs onto a tile grid may be unrecognizable
  (diagonals staircased, heights flattened). *(now: likely moot if D1 picks BSP over a grid; otherwise the
  honest fallback is "DOOM-flavored maps with real DOOM assets.")*

## §G — Gaps & scope cuts (G-codes; status)

- **G1** engine scope → hand-written FlipJump, no c2fj. **G2** blit cost → device reads framebuffer from
  memory (~0 FJ ops; 1.5.0 cmd `0x03`). **G3/G15** divides/fixed-point → reciprocal/scale LUTs, no FP,
  8.8 where 16.16 is overkill. **G4** timing → virtual frame-counter clock, no timer device. **G5** memory
  → flat-path, compact layout (Part I §3.3). **G6** interpreter fast-mode → *resolved by 1.5.0 native
  engine.* **G7** WAD pipeline → R0 converter + gridification/licensing (shareware dev, Freedoom
  redistribution). **G8/G23** tiny-res features → minimal = 3D view only, auto-warp, skip menu/HUD
  (*re-baseline:* HUD/text now plausible at 160×100, D7). **G9** audio → out of scope. **G11** `@` growth
  → keep hot data low/`pad`-aligned, re-measure (=U7). **G13** variable frame cost → set worst-case scene
  or accept fps swing (D9). **G14** BSP pointer-heavy → *re-baseline: now affordable, D1.* **G16** cache
  coherence with self-modifying code → *resolved inside the 1.5.0 engine.* **G17** device↔memory hook →
  *shipped (DeviceMemory).* **G18** framebuffer layout → packed, device-readable (D3 settles encoding).
  **G20** assembler scalability → *upgraded to a dependency, R-2.* **G21** tic/render decoupling → render
  1-of-N tics if needed (D9). **G22** flat-mode reference → host-side reference model of our exact
  renderer+sim (§H). **G25** sim scope → S0/S1/S2 ladder (§D). **G26** headless-first dev → *shipped
  (`PcIO.headless`, `InMemoryScreen`).*

## §H — Verification approach (carried forward)

- **TDD everywhere:** a unit test per macro (FixedIO/byte-exact, `--werror`, **a boundary input per
  behavior path** — a single green fixture proved insufficient three times in the catalog), per generated
  table, per device interaction. Register macro tests in the catalog/hexlib style so existing infra runs
  them.
- **Per-table tests:** each generated `.fj` table diffed against a host-reference over many indices,
  including first/last entries and wrap boundaries.
- **Budget + compile budget tracked together:** `op_counter`/frame (profiling mode / featured loop on
  small builds) **and** assemble time + `.fjm` size, at every milestone. Micro-benchmark in isolation first
  (a Lever-0 packed fill; a per-column-math path) before trusting any tier promise.
- **Correctness by reference model:** dump `SCREEN→PNG` (headless device) and **hash + diff against the
  host-side flat/textured reference**; the sim's state (player pos/angle after a scripted input sequence)
  must match the reference exactly. Per-region op-count profiling localizes overspend.
- **End-to-end:** `fj doom.fjm --io pc` interactively, or `PcIO.headless(events_file, frames_dir)` with a
  **scripted key-event file** for CI — scene renders, movement/collision/fire behave per the reference,
  measured fps (device present-log) meets the tier; captured frames/video are the deliverable.

## §I — Open questions inherited (mapped to Part I's D-backlog)

The pre-1.5.0 OQ list is now folded into Part I's D1–D15; the still-empirical ones: **OQ4** (does the
per-column math reduce fully to LUTs + adds, or leave residual mul/div? → D2/R1), **OQ5** (16.16 vs 8.8 per
quantity, wobble vs cost → D6), **OQ8** (are map/texture dispatch tables small enough for the compile +
span budget? map likely yes, textures open → D5/R-2/R-3), **OQ9** (`fcall` non-reentrancy — does any
hot-path call chain exceed one nesting level, forcing a stack? → R1 as the call graph forms), **OQ10**
(accept variable fps or cap to a worst-case scene? → D9/R2). Resolved by 1.5.0: interpreter speed (was
OQ1), w-width (OQ2 → w=32 confirmed), device/cache/debugger questions (shipped).
