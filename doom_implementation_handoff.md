# DOOM-on-FlipJump — Implementation Handoff (post-1.5.0)

**This document supersedes `doom_on_flipjump_plan.md` wherever they disagree — especially every
performance number.** The old plan was written against a ~2–4M fj/s pure-Python interpreter; flipjump
1.5.0's native engine re-baselined the world by ~50–80×. The old plan remains valuable for its cost
models, risk catalog, and methodology; its budgets, tier table, and "interpreter is the gate" framing are
obsolete.

**Where the work happens:** the game lives in the **`tomhea/doom-flipjump`** companion repo. The
`flipjump` package (`>=1.5.0`, `[io]` extra for the pygame device) is a pinned dependency — we do not
modify it except via its own finish-up handoff. Sessions working on the game need that repo in scope
(and the `flipjump-dev` skill installed, or clone `tomhea/skills` and read
`plugins/flipjump/skills/flipjump-dev/SKILL.md` + `reference/`).

---

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
- **IO devices**: whole-device `--io MODE` (`standard` terminal default; **`pc`** = pygame window with
  live keyboard + scaled 256-color screen, F11 fullscreen, window-close = clean stop). Headless/CI:
  **`PcIO.headless(events_file, frames_dir)`** replays scripted key events and writes PNG frames;
  **`InMemoryScreen`** captures the screen command stream. A **`DeviceMemory` hook** lets devices
  read/write program memory mid-run (the screen reads pixels straight from memory). Mode strings carry
  factory params (`--io "pc <params>"`) — no parallel config channel, ever.
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

### 1.3 What the old plan got that 1.5.0 deleted (the delta table)

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

Still standing from the old plan: the per-op cost intuitions (pointer ≫ fixed-address; div ≫ mul;
variable shifts are pointer-class — U6), the TDD methodology + `hex.scmp` rule, the compositor/pass
architecture, the reference-model verification idea, `@`-sensitivity (U7), and 32×32→64 fixed-point
intermediates (U5).

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
  `FLIPJUMP_FLAT_MAX_WORDS`); cost is RAM (8B × span) + fill time only. Document the knob and the
  paged fallback in the doom README.
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
  (scope ladder S0–S2 from the old plan, re-budgeted), combat, level transitions.
- **Present layer** — init/set_palette/update_screen; `update_rectangle` only for status-bar/menus.
- **HUD/status bar/menu/text passes** — now plausible at 160×100 (D7); compositor/pass pipeline and
  the `blit_rect`/glyph design carry over from the old plan.
- **Debug/diagnostics** — op-count probes, frame dumps, on-screen debug values (cheap at this budget).

**Decision backlog** (the Q&A agenda; owner leanings recorded where known):
- **D1 — Visibility:** BSP walk (real DOOM maps, now affordable) vs grid raycaster (simpler, proven
  in old plan). Settles map compiler + U11's fate.
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
flipjump 1.5.0 tagged (incl. finish-up: WI-E assembler, WI-G raw-frame,
                       WI-G2 flat config, WI-H release)
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
4. Read, in order: this handoff → `doom_on_flipjump_plan.md` (cost models/risks; numbers superseded)
   → `flipjump/stl/hex/tables_init.fj` (the dispatch-LUT mechanism) → PR #1's diff.
5. Start Stage 1: open `DESIGN.md` in doom-flipjump, seed it from §5, and begin the Q&A.
