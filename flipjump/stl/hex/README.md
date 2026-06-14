# `hex/` — hex-nibble macros

This folder is the table-driven counterpart to `bit/`. Where `bit/`-macros work one bit at a time, `hex/`-macros operate on 4-bit nibbles via precomputed truth tables, trading constant program-size overhead for `~4×` faster per-nibble arithmetic.

## What lives here

| File / folder           | What it provides                                                                                  |
|-------------------------|---------------------------------------------------------------------------------------------------|
| `tables_init.fj`        | `hex.init` and the `tables.*` machinery that every other hex operation depends on.                |
| `memory.fj`             | `hex`, `vec`, `zero`, `mov`, `xor_by`, `set`, `swap`.                                             |
| `logics.fj`             | `xor`, `or`, `and`, `not`, plus `exact_*`, `double_xor`, `quadrupled_exact_xor`.                  |
| `math.fj`               | `add`, `sub`, `add_constant`, `add_shifted`, `add_hex_shifted_constant`, plus the `clear_carry` / `not_carry` / `set_carry` helpers for both `ns add` and `ns sub`. |
| `math_basic.fj`         | `inc`, `dec`, `neg`, `abs`, `inc1`, `dec1`, `step`, `add_count_bits`, `count_bits`, `sign_extend`. |
| `mul.fj`                | `mul`, `mul10`, `add_mul`, plus the per-multiplication carry / init machinery.                    |
| `div.fj`                | `div` (unsigned) and `idiv` (signed, with configurable remainder convention).                     |
| `shifts.fj`             | `shl_hex`, `shr_hex`, plus the inline-table `shl_bit_once` / `shr_bit_once` helpers.              |
| `cond_jumps.fj`         | `if`, `if0`, `if1`, `if_flags`, `sign`, `cmp` (3-way), `cmp_eq_next`, `min`/`max`, `cmp.init`.    |
| `input.fj`              | `input_hex`, `input` (1 hex / n hexes), `input_as_hex`, `input_dec_uint`/`input_dec_int`, `input_dec_uint_until`/`input_dec_int_until`. |
| `output.fj`             | `output`, `print`, `print_as_digit`, `print_uint`, `print_int`, `print_digit`, `print_dec_uint`, `print_dec_int`. |
| `strings.fj`            | `input_ptr_line`, `print_ptr_text`, `print_ptr_line` (packed-byte line/text I/O), plus `fill_bytes` / `copy_bytes` block helpers. |
| `casting.fj` *(root)*   | `bit2hex` / `hex2bit` — bridging the two namespaces.                                              |
| `pointers/`             | The hex pointer subsystem (see below).                                                            |

## `pointers/` subfolder

| File                              | What it provides                                                                |
|-----------------------------------|---------------------------------------------------------------------------------|
| `basic_pointers.fj`               | `ptr_init`, `ptr_jump`, `set_flip_pointer`, `set_jump_pointer`, `stack_init`.   |
| `pointer_arithmetics.fj`          | `ptr_inc`, `ptr_dec`, `ptr_add`, `ptr_sub`.                                     |
| `read_pointers.fj`                | `read_byte`, `read_hex`, `read_byte_and_inc`, `read_hex_and_inc`.               |
| `write_pointers.fj`               | `write_byte`, `write_hex`, `write_byte_and_inc`, `write_hex_and_inc`, `zero_ptr`. |
| `stack.fj`                        | `push`, `pop`, `push_byte`/`hex`/`ret_address`, `pop_*`, `sp_inc/dec/add/sub`.   |
| `xor_from_pointer.fj`             | `xor_byte_from_ptr`, `xor_hex_from_ptr`, `read_byte_from_inners_ptrs`.          |
| `xor_to_pointer.fj`               | `ptr_flip`, `ptr_wflip`, `ptr_wflip_2nd_word`, `xor_*_to_ptr`, `xor_*_to_flip_ptr`. |

## Conventions

- A **hex** is one 4-bit nibble at one `dw`-aligned address — value `0..15`.
- A **`hex[:n]`** (or `hex.vec n`) is `n` nibbles, slot `i` at address `base + i*dw`. `n > 0` is always assumed — passing `n = 0` to any vector macro is undefined behaviour.
- Almost every hex operation **requires one of the init macros** to set up its truth table — either the specific `hex.<op>.init`, or `hex.init` (which calls them all). Macros that need init carry a `// @requires hex.<op>.init (or hex.init)` line.
- The `tables.ret` / `tables.res` shared symbols are allocated by `hex.tables.init_shared` (called from `hex.init`).
- Pointer macros require `hex.pointers.ptr_init`; usually bundled into `stl.startup_and_init_all`.
- Macros with non-obvious preconditions (e.g. `times <= n` in shifts, `bit_shift % 4 == 0`) carry a `// @Assumes:` line.
- Variables intended as data (`hex`, `hex.vec`) must be placed in a region of memory that is **never executed** — typically below `stl.loop`. Running them as-is is undefined behaviour.

For the rendered, navigable reference of every macro see <https://fjdocs.tomhe.app/stl/hex/>.
