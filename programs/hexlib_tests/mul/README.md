# `hexlib_tests/mul/` — Multiplication tests

Tests for the `hex.mul` and `hex.add_mul` (multiply-accumulate) macros. Each test
is split into a small **data file** (operand size, repetition count, sign flag,
and random test vectors) compiled together with a shared **harness** that runs
the macro and prints the results (see `tests/tests_tables/test_compile_hexlib.csv`).

| File | Purpose |
|---|---|
| `mul_test.fj` | Harness for `hex.mul`. |
| `mul16.fj` / `mul32.fj` / `mul64.fj` | `hex.mul` data for 16/32/64-bit operands. |
| `mul32_negative.fj` | `hex.mul` data for signed (negative) 32-bit operands. |
| `add_mul_test.fj` | Harness for `hex.add_mul`. |
| `add_mul4.fj` / `add_mul8.fj` / `add_mul32.fj` / `add_mul64.fj` | `hex.add_mul` data for 4/8/32/64-bit operands. |
| `add_mul_n.fj` | `hex.add_mul n` at n = 1, 2, 3, 4, 7, 8, 16, with a dirty multiply-carry on entry (standalone). |
| `fixed_point.fj` | `hex.fixed_mul` / `fixed_mul_low` / `fixed_div`, `hex.mul_low` and `hex.mul_const` (standalone). |
