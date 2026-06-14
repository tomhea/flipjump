# Interpreter benchmark results

Benchmark: `python tests/benchmarks/benchmark_interpreter.py 2000` — `prime_sieve.fj`, sieve up to 2000,
`FixedIO` input, statistics timer (IO-paused) as the denominator.

## Baseline — fj==1.4.0 interpreter (commit 4da18b7)

Hardware: Windows 11 Pro, CPython 3.11.0.

| width | ops        | time   | speed        |
|-------|-----------:|-------:|-------------:|
| w=32  |  6,753,189 | 40.10s | 168,418 fj/s |
| w=64  | 13,486,652 | 90.93s | 148,321 fj/s |

Calibration: a minimal synthetic dict-based fetch/flip/jump loop runs at ~4.9M ops/s in CPython
on this machine — the upper bound for any pure-Python interpreter here. The ≥10M fj/s target
therefore requires the native fallback (MSVC Build Tools available on this machine).

## Python fast-loop (fast-run mode, the fallback default)

The run-loop with inlined memory accesses and IO/termination checks, per-op statistics skipped.

| width | ops        | time   | speed          | vs baseline |
|-------|-----------:|-------:|---------------:|------------:|
| w=32  |  6,753,189 |  1.75s | 3,866,762 fj/s |       23.0x |
| w=64  | 13,486,652 |  9.15s | 1,474,358 fj/s |        9.9x |

w=64 is markedly slower than w=32: addresses are >2^60 (prime_sieve's table sits at 1<<63), so
every int is a multi-digit CPython PyLong, and the small-int fast paths don't apply.

## Native engine (_fjcore C-extension, the default when built)

Segment-aware paged memory (lazily-allocated 128KB pages) + the run-loop in C; Python is
called back only for IO. Build with `pip install -e .`.

The shipping engine (v10 below; sieve = hybrid path - low code flat, far data table
paged; loop = compact/flat path):

| program            | width | ops           | speed             | vs baseline |
|--------------------|-------|--------------:|------------------:|------------:|
| sieve (n=5,000)    | w=32  |    16,580,560 |  ~244-276M fj/s   |      1,565x |
| sieve (n=5,000)    | w=64  |    33,304,073 |  ~272-278M fj/s   |      1,855x |
| sieve (n=200,000)  | w=64  | 1,332,300,215 |  ~315M fj/s       |      2,124x |
| loop (compact)     | w=32  |   298,927,147 |  ~410-440M fj/s   |      2,553x |
| loop (compact)     | w=64  |   351,500,749 |  ~410-443M fj/s   |      2,985x |

(the sieve hot marking loop runs at ~315-320M; SMALLER n is slower - the fixed startup
cost (stl init, hex-table build, reading n) is a large fraction of a short run, and larger
n amortizes it. measured sweep, isolated/cool: n=5k 272M, 10k 291M, 20k 309M, 40k 318M,
then a ~315M plateau through 200k. so the sustained 1.3B-op run is at the ceiling and is
FASTER than the short n=5k run, not slower.

THERMAL CAVEAT (this laptop): a multi-second run on a pre-heated core throttles to ~half
clock - an earlier "170M / +13% / DRAM-bound" note here was that artifact (the same 1.33B
ops took 7.96s hot vs 4.2s cool). measure long runs isolated, with the machine settled;
trust wall-time over the post-run freq-probe, which only catches the recovered boost clock.)

**The ≥10M fj/s acceptance is met with ~10x margin on every row.**

## Native-engine optimization history (i7-12700H, P-core ~4.6GHz)

| step | sieve w=32 | sieve w=64 | sieve w=64 1.3B-op | loop w=32 (compact) |
|---|---:|---:|---:|---:|
| v1: 1-entry page cache | 78M | 84M | 40M | - |
| v2: 16-way direct-mapped page cache | 96M | 97M | 104M | - |
| v3: one page lookup per op-pair | 127M | 129M | 140M | 123M |
| v4: flat storage for compact programs | 112-127M | 115-129M | 125-140M | 246M |
| v5: dedicated flat loop (no ring/paged branches) | - | - | - | 273M |
| v6: + MSVC PGO (optional; build path since removed) | 122M | 128M | 132M | **280-286M** |
| v7: slim specialized flat loop (cold-block layout, folded IO checks, per-width constants, strip-mined signal check) | unchanged | unchanged | unchanged | **352-430M** (+25-30% interleaved A/B) |
| v8: flat storage opened to w=64 (magic gap sentinel + segment-routed API) | unchanged | unchanged | unchanged | loop w=64: 110M -> **358-382M** (3.4x) |
| v9: slim paged loop (widened page cache, cold-block reshape, width+ring clones) | **~180M** (+50%) | **~180M** (+50%) | - | paged-forced w=64: 130M -> **221-239M** (+75-85%); flat unchanged |
| v10: hybrid storage (low flat window + paged far data) | **269M** | **279M** | **315M** | flat unchanged (~410M/435M) |

- v2: the ip/jump page and the flip-target page alternated every op and thrashed the single
  cached entry, forcing a hash lookup per access.
- v3: an op's flip-word and jump-word are adjacent (word_address, word_address+1) - one page
  lookup serves both (~30%).
- v4 (flat storage): programs whose segments all end below 8M words, with garbage-stop,
  get one dense array instead of the page table; out-of-segment words carry a
  bit-63 sentinel (w<=32 until v8 - see below). This removes the page lookup from the serial jump-dependency chain
  (jump-word load -> next op's address -> next load), which bounds the loop: ~2x on
  compact-memory programs. prime_sieve declares a half-address-space segment, so before
  v10 it stayed wholly on the paged path; v10's hybrid mode runs its low code flat while
  the far table stays paged. `FLIPJUMP_NO_FLAT=1` forces the fully-paged path (for A/B
  measurement).

- v5: when the memory is flat and no last-ops ring is requested (the shipping default),
  a dedicated loop runs with zero paged-mode/ring branches in the per-op path.
- v6: profile-guided optimization is available for local builds
  (`--pgo-instrument`, train on both the flat and paged paths, `--pgo-use`);
  the prebuilt wheels ship without it.
- v10 (hybrid storage): the dichotomy is dissolved - in garbage-stop mode the LOW WINDOW
  (segment data below the flat-words limit) always gets a dense flat array, and anything
  above stays page-backed. when every segment fits the window the mode is `flat` (the
  historic behavior, codegen untouched); otherwise it is `hybrid`. FJ code is laid out
  low and far segments hold data (sieve's table at 1<<63, framebuffers), so the per-op
  f/j fetches - the serial chain that dictates speed - run on the flat array even for
  sieve, while the occasional flip into the far table (a store, off the critical chain)
  takes the paged machinery through the now-widened page cache. the boundary check costs
  nothing on the hot path: the flat loop already tested `word_address + 1 >= flat_count`
  to mark garbage, and that cold edge now falls into the paged helpers (mem_read_word /
  mem_flip_bit) instead of erroring - exact paged semantics above the cut, including
  out-of-segment errors and device pokes (set_word/get_word route in-segment+below-cut to
  flat, everything else to pages). isolated freq-locked A/B (FLIPJUMP_NO_FLAT off vs on),
  sieve median fj/s: w=32 hybrid 269M, w=64 hybrid 279M vs paged 162M (+72%); the
  sustained 1.3B-op run (isolated/cool) is hybrid ~315M vs paged ~180M (+75%) - and is
  faster than the short n=5k run, which is startup-cost-limited, not slower. the
  flat paths are untouched (loop 410M/435M). a program with no segment below the window
  (purely far) degenerates to plain paged, unchanged.
- v9 (slim paged loop): the generic loop got the v7 treatment plus a cache widening.
  the 16-way page cache now holds the page's words pointer and fast valid range next to
  the key, so the hot path reads op words without chasing the Page* (one less chained
  load on the serial ip path). the loop itself is the run_paged_loop_impl reshape: rare
  conditions (unaligned/straddling ops, cache misses, outside-valid-range words, IO,
  termination) are forward gotos to cold blocks, the IO range tests fold to one unsigned
  compare each, the signal check is strip-mined, width/ww arrive as literals from a
  dispatch, and the last-ops ring + the flat-with-ring debug lane are const-folded out of
  the common (no-ring) clones. anything outside the fast path falls back to the original
  helpers (mem_read_word / mem_flip_bit validate per word) - exact original semantics.
  this is the floor for programs that cannot take flat storage (far/huge/sparse
  segments, e.g. sieve): interleaved PGO A/B - paged-forced loop w=64 130M -> 221-239M
  (+75-85%), sieve w=64 108-126M -> 174-186M (+50%); the flat paths are untouched.
- v8 (w=64 flat): flat storage was limited to w<=32 because the garbage sentinel lived
  in-band at bit 63 - a w=64 value uses all 64 bits. gaps are now filled with a fixed
  magic constant instead (FLAT_GARBAGE_MAGIC, sqrt(3)'s fractional bits); a real word
  that happens to equal it is disambiguated through the segment list on the cold path
  (gap words are never legally written, so in-segment + magic == real data) - the hot
  path stays one register compare, zero extra loads, and the w<=32 instantiations keep
  the in-band sentinel with their codegen untouched. the API accessors (set_word /
  get_word, i.e. the device-memory hook and the debugger) route by segment membership -
  in-segment words live in the flat array, everything else stays page-backed (the pages
  are kept at the flat build) - so flat mode is device-transparent exactly like paged
  mode. programs reserving far/huge segments (a sieve table at 1<<63) still fail the
  flat-limit check and keep the paged path. w=64 is the default width, so most compact
  programs get this for free: interleaved PGO A/B, loop w=64: 110M -> 358-382M fj/s
  (**3.4x**); loop w=32 and sieve (paged) unchanged.
- v7 (slim flat loop): the run-loop body is reshaped for the hot path - every rare
  condition (unaligned ip, out-of-span words, garbage sentinels, IO hits, termination)
  is a forward goto to a cold block, so the common op runs straight-line with all
  conditional branches fall-through and one taken branch (the strip-mined back-edge);
  the two IO range tests fold to one unsigned compare each; `run_flat_loop` dispatches
  to a force-inlined body with literal width/ww per supported width, so shifts and
  IO-range constants are immediates (the generic version reloaded ~10 spilled stack
  slots per op). Measured by interleaved same-hour A/B against the pre-change commit,
  both PGO: 352-430M vs 295-348M. The remaining bound is the memory-disambiguation
  stall around flip-store -> jump-load (structural to FJ semantics - see the
  speculation post-mortem below).

Cycle accounting at ~4.6GHz: v1 was ~46 CPU-cycles per fj-op, v6-flat is ~16. The serial
dependency floor (jump-word load -> address arithmetic -> next jump-word load, L1-resident)
is ~7-8 cycles/op (~600M fj/s); the paged-path floor is ~13-15 cycles (~330M fj/s).

(prime_sieve at w=32 is limited to n <= ~5792: its mark-pointer `p*p*dw` wraps the 2^32-bit
address space beyond that — a property of the program, reproduced identically on all engines.)

### Re-measurement after the configurable-flat-limit work (PGO rebuild, June 2026)

After the configurable flat limit + storage-mode getter changes (none on the per-op
paths) the engine was PGO-rebuilt (trained
on both loop and sieve) and re-measured: **loop w=32 334M fj/s** (above the recorded
280-286M), sieve w=32/w=64 ~97-105M on short runs, and 72M on the sustained 1.3B-op run.
The sustained-run gap vs the recorded 132M is machine-state, not code: the pre-change
commit was rebuilt and measured in an A/B worktree the same hour and gives the same 71M -
long runs on this laptop decay from boost clocks. Short-run paged numbers sit within the
historical 96-140M band.

## Assembler speedup

Benchmark: `python tests/benchmarks/benchmark_assembler.py` - three workload shapes: hello_world.fj
(the per-program fixed cost), prime_sieve.fj (macro-heavy), and a generated 64K-entry
byte-LUT program (data-heavy, the mega-data-table shape). Acceptance: bit-identical .fjm
outputs - verified by sha256 on 14 outputs (10 catalog programs across categories + the 3
workloads, w=64 and w=32), cold- and warm-cache, before vs after.

What changed (all output-preserving):
- **Expr sharing** (`expr.py`): `eval_new` returns unchanged (sub)expressions as-is instead
  of deep-cloning them - Expr objects are immutable, so trees share nodes. Substituting a
  parameter is now a node swap, not a clone of the parameter's tree. Single-pass
  fold/unchanged detection. `FlipJump`/`WordFlip` ops likewise return themselves when
  nothing was substituted. This was the dominant macro-resolve cost (~1.1M `eval_new`
  calls assembling prime_sieve).
- **stl-prefix parse cache** (`fj_parser.py`): the parser state after the stl files (the
  consts + macros dicts and the main macro's ops) is snapshotted once per
  (stl files mtime/size, w, werror) and reused by every later assemble in the process.
  The parsed Macro/op objects are immutable from then on (the preprocessor
  clones-or-shares, never mutates), so sharing them is safe. This removes the ~0.13s
  per-program fixed cost the catalog used to pay ~1,000 times.
- `exact_eval` fast paths (labels resolve); bulk `struct.pack` of the data words and a
  lazy error-string in `add_segment` (fjm_writer).

| workload | before | after | speedup | now dominated by |
|---|---:|---:|---:|---|
| hello_world | 0.149s | 0.020s (warm) | 7.5x | stl parse (cold only) |
| prime_sieve | 2.35s | 1.58s | 1.5x | macro resolve + lzma |
| lut64k (64K-entry LUT) | 5.31s | 4.03s | 1.3x | SLY parsing of 64K data lines |
| catalog compile (1,029 programs) | ~14 min | 9:03 | 1.55x | per-program macro resolve |

(the lut64k generator was later slimmed to a data-only program - the absolute numbers of a
fresh `benchmark_assembler.py` run are lower than this table, measured with the original
generator; the before/after comparison above used the same workload on both sides.)

Known remaining costs (documented, not pursued): the SLY lexer/parser's pure-python
per-token overhead dominates mega-table source files (~2.4s of lut64k - generating tables
as fewer, longer lines or assembling them once into a library would sidestep it); LZMA
compression (preset 6) is most of "create binary" and is part of the .fjm format itself.

## Jump-target speculation: miss-rate study. Verdict: **GO**

The native engine is bounded (~16 cycles/op flat) by the serial chain: this op's jump-word
load -> next op address -> next load. Jump-target speculation (remember the last jump target
per op address, start the next op's loads early, verify) only pays if the jump word at a
given ip rarely changes between executions. Measured with the exact counting mode in
`_fjcore` (`FLIPJUMP_MEASURE_SPECULATION=1`, off the normal hot path; reproduce with
`python tests/benchmarks/measure_speculation.py`):

| program | ops | first executions | misses | miss-rate | warm miss-rate |
|---|---:|---:|---:|---:|---:|
| loop w=32 (flat) | 298,927,147 | 387 | 22,666,658 | 7.58% | 7.58% |
| loop w=64 (paged) | 351,500,749 | 497 | 22,666,658 | 6.45% | 6.45% |
| sieve w=32 n=5000 (paged) | 16,580,560 | 34,947 | 1,032,607 | 6.23% | 6.24% |
| sieve w=64 n=5000 (paged) | 33,304,073 | 59,960 | 1,884,740 | 5.66% | 5.67% |
| hexlib mul64 | 984,638 | 46,719 | 85,587 | 8.69% | 9.13% |
| hexlib add_mul64 | 197,868 | 45,808 | 15,549 | 7.86% | 10.23% |
| hexlib div test8_8 | 161,920 | 15,884 | 8,537 | 5.27% | 5.85% |
| hexlib idiv | 308,357 | 31,060 | 19,218 | 6.23% | 6.93% |

(A miss = an executed op whose jump word differs from its value on the previous execution of
the same ip; "warm" excludes each ip's first execution from the denominator.)

**Every workload sits at 5.3-8.7% - below the ~10% GO threshold.** The FlipJump property
holds as conjectured: most executed ops (truth-table cells, straight-line code) have jump
words that never change after init; wflip-mutability concentrates in few dispatch/return
cells. With a correctly-predicted op the next op's two loads start one serial-chain step
early, so the expected gain is +50-80% on the flat path (~450-600M fj/s).
**Verdict: GO - the speculation tier is worth building** (as future engine work).

### The tier was then built and measured: **REJECTED** (the slim loop shipped instead)

Three speculation variants were implemented in `run_flat_loop` and A/B-measured on the
loop benchmark (same build, `FLIPJUMP_NO_SPECULATION=1` as the off-switch; asm listings
inspected to confirm the intended codegen):

1. **Predict-and-prefetch** (prefetch `flat[pred]` after the jump word arrives): no gain -
   by the time the real jump word is in hand, the next loads issue anyway.
2. **1-ahead value speculation** (`ip = pred` on a verified hit, so the next iteration's
   address comes from the predictor load instead of the jump-word load): no gain, then a
   **-22% loss** once the loop was slimmed. The flaw in the study's premise: the predictor
   load (`spec_pred[word_address/2]`) has the same L1 latency as the jump-word load it
   replaces, so the serial chain does not get shorter - and the bookkeeping (table load,
   verify branch, miss store) costs real throughput in a lean loop.
3. **Early jump-word loads** (read `flat[word_address+1]` before the flip store, fix up
   the self-flip case from the register): **-40%**. The wflip dispatch idiom routinely
   writes the *next* op's jump word right before it executes, so an early load races the
   in-flight store and triggers memory-order machine clears. The engine's post-flip read
   order is load-bearing.

What the experiments exposed instead: the loop was throughput-bound (~59 instructions,
~6 taken branches, ~10 stack reloads per op in the MSVC codegen), not latency-bound -
fixing that is the v7 slim loop above. The remaining ~9-11 cycles/op carry the
flip-store -> jump-load disambiguation stall, which is structural to FlipJump semantics
(the flip may modify the jump word; both escapes from the stall are the rejected
variants 2 and 3). Decision: **no speculation tier**; the predictor infrastructure was
removed, the measuring mode (`FLIPJUMP_MEASURE_SPECULATION=1`) stays as a study tool.

### Bottleneck decomposition (the oracle-replay study)

Where do the slim loop's ~10.3 cycles/op go (loop w=32, 4.2GHz sustained - measured with
a chained-add frequency probe)? Three instrumented experiments (scratch science builds,
not committed; the methods are reproducible from this description):

1. **Dispatch-aliasing frequency**: counting how close each op's loads are to recent flip
   stores: **7.14% of ops flip the very next op's jump word** (the wflip dispatch idiom),
   matching the speculation study's 7.58% miss rate - they are the same ops. f-words are
   never recently written; j-words only by distance-1 (7.14%) and distance<=16 (10.5%).

2. **Oracle replay**: record the 50M-op ip trace, then re-run the identical op sequence
   with the next ip taken from the linear trace array instead of the loaded jump word -
   i.e. a perfect, zero-miss, prefetch-friendly speculation oracle, on identical machine
   code (runtime mode flag in one shared clone). Result: the oracle is *slower* (288M vs
   377M fj/s) - removing the serial ip chain entirely buys nothing. Since no speculation
   scheme can beat its own oracle, this closes speculation on this engine for good (any
   correct scheme must still load the jump word to verify, which is the oracle's exact
   work).

3. **Iso-codegen ablations** (replay-driven, so control flow cannot diverge): per-op cost
   of each component - IO range checks ~1.7 cycles, flip RMW ~3.8 (store ~1.7 + load
   ~2.1), jump-word load + finish checks ~3.5, loop skeleton + flip-word load ~7.8 floor
   (components overlap under OOO; the sum exceeds the total).

**Conclusion: the engine is bound by its per-op memory work - 3 loads + 1 RMW store,
with 7.14% of jump-word loads needing store-to-load forwarding of a just-written word -
not by any dependency chain.** Interpreter-level tricks that only shorten the chain
(prediction, prefetch hints) are oracle-bounded below current performance. The lever
that remains is removing interpreter work per op: binary translation (each FJ op as
2-3 host instructions; wflip-dispatch ops as per-site indirect jumps, which the BTB
predicts at the measured 92-95%) - a post-1.5.0 engine direction.


## OQ-A1 — decoded-op cache: measured, and rejected

A decoded-op cache prototype (cache `ip -> (flip_word_addr, flip_bit, jump_word_addr)`, with
the address-keyed invalidation dirty-check on every flip) was measured against the direct
loop in an op-shaped synthetic benchmark (CPython 3.11):

- w=32-like ints: direct 7.32M op/s, cached 8.88M op/s (**1.21x**)
- w=64-like ints: direct 5.70M op/s, cached 7.24M op/s (**1.27x**)

The dirty-check (one dict membership test per flip) eats roughly half of what the cache saves.
Diluted by the rest of the real loop (IO checks, termination checks, op counting), the
end-to-end gain is ~10% — and the pure-Python loop cannot approach 10M fj/s either way (the
synthetic no-op-body ceiling on this machine is ~4.9M). The native engine reads decoded words
from dense pages directly (no decode step exists), so a cache buys nothing there. Decision:
**no decoded-op cache**; the native engine is the speed path, the plain fast loop the fallback.

## w=32 vs w=64 — recommendation: **w=32** (for op-heavy programs)

- **Op count (dominant factor):** hex/bit STL macro costs scale with w (`hex.add` is O(w/4)
  ops, pointers/wflip are O(w) flips). The same algorithm runs ~2x fewer fj-ops at w=32 —
  prime_sieve(2000) is 6.75M ops at w=32 vs 13.49M at w=64.
- **Native-engine per-op speed:** roughly equal (78M vs 84M fj/s) — so halving the op count
  halves wall-time. End-to-end, w=32 is ~2x faster for the same program.
- **Python-fallback per-op speed:** w=32 is 2.6x faster per-op (PyLong digit effects), on top
  of the op-count halving.
- **Memory:** native pages store 8B/word regardless of w, so footprint is the same per touched
  word — but w=32 programs touch half the words. The .fjm file is also half the size.
- **Fixed-point fit:** 16.16 fixed-point fits w=32 exactly. The intermediate-width trap
  (U5): a 16.16 multiply needs a 64-bit product — two words + hand-carried overflow at w=32
  (extra ops in a fixed-point multiply), free at w=64. A LUT-heavy design keeps
  runtime multiplies rare, so this doesn't flip the verdict.
- **Address-space caveat:** w=32 has 2^32 bits of address space. Compact fixed layouts
  fit easily, but quadratic pointer arithmetic can wrap (see the prime_sieve note above) —
  keep address computations bounded.
