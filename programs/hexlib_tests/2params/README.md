# `hexlib_tests/2params/` — Two-operand hex macro tests

Tests for the two-operand `hex.*` macros. The plain files exhaustively cover
every single-hex digit pair; the `_n` files cover the multi-hex (vectored)
variants; the `_shifted` files cover adding/subtracting a shorter vector into a
longer one at every offset.

| File | Purpose |
|---|---|
| `add.fj` / `add_n.fj` / `add_shifted.fj` | `hex.add`: single-digit, multi-hex, and shifted. |
| `sub.fj` / `sub_n.fj` / `sub_shifted.fj` | `hex.sub`: single-digit, multi-hex, and shifted. |
| `and.fj` / `and_n.fj` | `hex.and`: single-digit and multi-hex. |
| `or.fj` / `or_n.fj` | `hex.or`: single-digit and multi-hex. |
| `cmp.fj` / `cmp_n.fj` | `hex.cmp`: single-digit and multi-hex. |
| `chain_n.fj` | the chained n-hex ops (`add`/`sub`/`or`/`and` n, `add_shifted`/`sub_shifted`) at n = 1, 2, 3, 4, 7, 8, 16, with carries and borrows through every nibble. |
