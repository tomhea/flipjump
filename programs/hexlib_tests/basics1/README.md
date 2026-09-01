# `hexlib_tests/basics1/` — Basic hex macro tests

Tests for the foundational `hex.*` macros: arithmetic, memory operations,
conditionals, input and printing. Each program runs its macro over a batch of
values and prints the results for comparison against the recorded `.out`.

| File | Purpose |
|---|---|
| `basic_math.fj` | Basic arithmetic: `inc1`, `inc`, `not`. |
| `basic_memory.fj` | Memory ops: `xor`, `zero`, `mov`, `set`, `swap`. |
| `if.fj` | Conditionals: `hex.if_flags` and `hex.if`. |
| `input.fj` | Hex input macros, including the error-checking variant. |
| `print_as_digit.fj` | `hex.print_as_digit` (lowercase and uppercase). |
| `print_int.fj` | `print_uint` and `print_int` on signed/unsigned values. |
| `triple_exact_xor.fj` | `hex.triple_exact_xor` over every source value `0..15`. |
