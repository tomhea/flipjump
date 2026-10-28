# `hex/` — hex-nibble macros

This folder is the table-driven counterpart to `bit/`. Where `bit/`-macros work one bit at a time, `hex/`-macros operate on 4-bit nibbles via precomputed truth tables, trading constant program-size overhead for `~4×` faster per-nibble arithmetic.

## What lives here

| File / folder           | What it provides                                                                                  |
|-------------------------|---------------------------------------------------------------------------------------------------|
| `tables_init.fj`        | `hex.init` and the `tables.*` machinery that every other hex operation depends on (incl. the chained table jumps of the n-hex ops). |
| `memory.fj`             | `hex`, `vec`, `zero`, `mov`, `xor_by`, `set`, `swap`.                                             |
| `logics.fj`             | `xor`, `xor_zero`, `double_xor`, `or`, `and`, `not`, plus the `exact_xor` family (`exact_xor` / `double_exact_xor` / `triple_exact_xor` / `quadrupled_exact_xor`: 1/2/3/4 destination groups) and its `address_and_variable_*` forms; the `sparse_*` forms (`sparse_xor`, `sparse_exact_xor`, `sparse_zero`, `sparse_mov`: their tables aligned to PAD ops); `or.init`, `and.init`. |
| `math.fj`               | `add`, `sub`, `add_constant` / `sub_constant`, `add_shifted` / `sub_shifted`, plus in both `ns add` and `ns sub`: `add_hex_shifted_constant` / `sub_hex_shifted_constant`, `add_constant_with_leading_zeros` / `sub_constant_with_leading_zeros`, the `clear_carry` / `not_carry` / `set_carry` / `chain` helpers, and `init`. |
| `math_basic.fj`         | `inc`, `dec`, `neg`, `abs`, `inc1`, `dec1`, `inc.step`, `dec.step`, `add_count_bits`, `count_bits`, `sign_extend`. |
| `mul.fj`                | `mul`, `mul10`, `add_mul`, `mul_low`, `mul_const`, `fixed_mul`, `fixed_mul_low`, plus the per-multiplication carry / init machinery (`mul.chain`, `mul.clear_carry`, `mul.chained_clear_carry`, `mul.chained_add_mul`, `mul.chain_step`, `mul.init`, `mul_low.row`, `mul_const.step`). |
| `div.fj`                | `div` (unsigned), `idiv` (signed, with configurable remainder convention), and `fixed_div`.       |
| `shifts.fj`             | `shl_bit`, `shr_bit`, `shl_hex`, `shr_hex`, plus the inline-table `shl_bit_once` / `shr_bit_once` helpers. |
| `cond_jumps.fj`         | `if`, `if0`, `if1`, `if_flags`, `switch` (16-way), `sign`, `cmp` (3-way), `scmp` (signed), `cmp_eq_next`, `min`/`max`, `cmp.init`. |
| `input.fj`              | `input_hex`, `input` (1 hex / n hexes), `poll_key` (the keyboard device), `input_as_hex`, `input_dec_uint`/`input_dec_int`, `input_dec_uint_until`/`input_dec_int_until`. |
| `output.fj`             | `output`, `print`, `print_as_digit`, `print_uint`, `print_int`, `print_digit`, `print_dec_uint`, `print_dec_int`. |
| `strings.fj`            | `input_ptr_line`, `print_ptr_text`, `print_ptr_line` (packed-byte line/text I/O), plus `fill_bytes` / `copy_bytes` block helpers. |
| `casting.fj` *(root)*   | `bit2hex` / `hex2bit` — bridging the two namespaces.                                              |
| `pointers/`             | The hex pointer subsystem (see below).                                                            |

## `pointers/` subfolder

| File                              | What it provides                                                                |
|-----------------------------------|---------------------------------------------------------------------------------|
| `basic_pointers.fj`               | `ptr_init`, `ptr_jump`, `set_flip_and_jump_pointers` (and its `set_flip_pointer` / `set_jump_pointer` aliases), `stack_init`, and the `wide_table_init`, `advance` / `advance_bit` internals. |
| `pointer_arithmetics.fj`          | `ptr_inc`, `ptr_dec`, `ptr_add`, `ptr_sub`, `ptr_index` (a signed full-width / an unsigned n-hex index; `pointers.ptr_index` / `ptr_index_narrow` / `ptr_index_wide` / `shl_index` are its steps). |
| `read_pointers.fj`                | `read_byte`, `read_hex`, `read_cell`, `read_byte_and_inc`, `read_hex_and_inc`, `read_cell_and_inc`, `read_nth_*`, `read_back`, `read_entry` / `read_entry_byte` / `read_entry_packed` (an entry of a constant array; `pointers.entry_address` computes its address). |
| `write_pointers.fj`               | `write_byte`, `write_hex`, `write_cell`, `write_byte_and_inc`, `write_hex_and_inc`, `write_cell_and_inc`, `zero_ptr`, `zero_cell_ptr`, `write_nth_*`, `write_back`, and the `pointers.write_cells` / `write_cell` / `advance_and_write_cell` steps. |
| `stack.fj`                        | `push`, `pop`, `push_byte` / `push_hex` / `push_ret_address`, `pop_byte` / `pop_hex` / `pop_ret_address`, `sp_inc` / `sp_dec` / `sp_add` / `sp_sub`. |
| `xor_from_pointer.fj`             | `xor_byte_from_ptr`, `xor_hex_from_ptr`, `xor_cell_from_ptr`, plus the `read_*_from_inners_ptrs` dances both decoder tables share, and the chained `xor_cells_from_inners_ptrs` / `read_cell_step` / `read_cell_chained`. |
| `xor_to_pointer.fj`               | `ptr_flip`, `ptr_flip_dbit`, `ptr_wflip`, `ptr_wflip_2nd_word`, `xor_*_to_ptr`, `xor_*_to_flip_ptr`, and in `ns pointers`: `xor_hex_to_ptr_and_inc` / `xor_byte_to_ptr_and_inc`, `advance_and_xor_to_flip_ptr`, `advance_by_one_and_flip__ptr_wflip`. |

## Conventions

- A **hex** is one 4-bit nibble at one `dw`-aligned address — value `0..15`.
- A **`hex[:n]`** (or `hex.vec n`) is `n` nibbles, slot `i` at address `base + i*dw`. `n > 0` is always assumed — passing `n = 0` to any vector macro is undefined behaviour.
- Almost every hex operation **requires one of the init macros** to set up its truth table — either the specific `hex.<op>.init`, or `hex.init` (which calls them all). Macros that need init carry a `// @requires hex.<op>.init (or hex.init)` line.
- The `tables.ret` / `tables.res` shared symbols are allocated by `hex.tables.init_shared` (called from `hex.init`).
- Pointer macros require `hex.pointers.ptr_init`; usually bundled into `stl.startup_and_init_all`.
- Macros with non-obvious preconditions (e.g. `times <= n` in shifts, `bit_shift % 4 == 0`) carry a `// @Assumes:` line.
- Variables intended as data (`hex`, `hex.vec`) must be placed in a region of memory that is **never executed** — typically below `stl.loop`. Running them as-is is undefined behaviour.

For the rendered, navigable reference of every macro see <https://fjdocs.tomhe.app/stl/hex/>.
