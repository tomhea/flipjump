# `hexlib_tests/basics2/` — Shift, bit-counting and pointer tests

Tests for the hex shift macros, the bit-counting macros and the pointer macros. The
plain `shl`/`shr` files cover small 2-hex values exhaustively; the `_n` / `_big` files
cover wide 16-hex values and variable shift amounts.

| File | Purpose |
|---|---|
| `shift_utils.fj` | Shared helper that compares a shift result against the expected value. |
| `shl_bit.fj` / `shl_bit_n.fj` | `hex.shl_bit` (shift-left by one bit): small / wide. |
| `shr_bit.fj` / `shr_bit_n.fj` | `hex.shr_bit` (shift-right by one bit): small / wide. |
| `shl_hex.fj` / `shl_hex_big.fj` / `shl_hex_n.fj` | `hex.shl_hex` (shift-left by whole hexes): small / wide / variable amount. |
| `shr_hex.fj` / `shr_hex_big.fj` / `shr_hex_n.fj` | `hex.shr_hex` (shift-right by whole hexes): small / wide / variable amount. |
| `count_bits.fj` | `hex.count_bits` over 64-bit values. |
| `add_count_bits.fj` | `hex.add_count_bits` (add while counting set bits). |
| `nth_pointers.fj` | `hex.ptr_index` and the indexed `read_nth_*` / `write_nth_*` macros. |
| `pointer_setters.fj` | The invariant tying `to_flip`, `to_jump` and their shared shadow together. |
| `wide_cells.fj` | A whole `hex.pointers.PTR_CELL_BITS`-wide cell written and read in one dereference. |
