# FlipJump Standard Library

> Prefer a click-through tour? Every macro below has a rendered, cross-linked, searchable page on the [**FlipJump Docs**](https://fjdocs.tomhe.app/stl/) site — go peek before you dive into the source.

The stl (**ST**andard-**L**ibrary) is a collection of FlipJump files, each a collection of **highly-optimized** and **[tested](../../tests/README.md)** macros, that are free to use by any FlipJump program that may benefit from it.

It mainly offers binary/hexadecimal data-structures, mathematical and logical operations, conditional jumps, pointers, casting, and input/output.

These FlipJump files result from a lot of research, runs, and many tests. 

**@note**: The faster mathematical macros are under the `hex` namespace.


# The Files

### [runlib.fj](runlib.fj)
This file contains constants and initialization macros.

Offers outputting constant chars/strings.

Macros: `startup`, `startup_and_init_pointers`, `startup_and_init_all`, `fj`, `wflip_macro`, `comp_if` / `comp_if0` / `comp_if1`, `comp_flip_if`, `skip`, `loop`, `output_bit`, `output_char`, `output`.

**@note**: It should be the first file in the compilation order.

### [bit/](bit)
Defines the `bit` data-structure (for binary variables).

Offers macros for manipulating binary variables and vectors (i.e. numbers):

- [memory.fj](bit/memory.fj) - bit/vec, zero, one, mov, swap
- [cond_jumps.fj](bit/cond_jumps.fj) - if, cmp
- [logics.fj](bit/logics.fj) - xor, xor_zero, or, and, not
- [casting.fj](bit/casting.fj) - casting between bits and ascii
- [input.fj](bit/input.fj) - input into bit variables
- [output.fj](bit/output.fj) - output (a bit), print (bytes), print_str, print_as_digit; print as hex/decimal (print_hex_uint/int, print_dec_uint/int)
- [shifts.fj](bit/shifts.fj) - bit-shift left/right, bit-rotate left/right
- [math.fj](bit/math.fj) - inc, dec, add, sub
- [mul.fj](bit/mul.fj) - mul10, mul, mul_loop
- [div.fj](bit/div.fj) - div10, {i}div, {i}div_loop (i stands for signed)
- [pointers.fj](bit/pointers.fj) - bit-vec pointers: ptr_flip, ptr_flip_dbit, ptr_wflip, ptr_wflip_2nd_word, ptr_jump, xor_to_ptr, xor_from_ptr, exact_xor_from_ptr, ptr_inc/ptr_dec; pointers init

### [hex/](hex)
Defines the `hex` data-structure (for hexadecimal variables).

**They are smaller and faster than 4 `bit`s.** 

Offers macros for manipulating hexadecimal variables and vectors (i.e. numbers):

- [memory.fj](hex/memory.fj) - hex/vec, zero, xor_by, set, mov, swap
- [cond_jumps.fj](hex/cond_jumps.fj) - if, switch (16-way), cmp, scmp (signed), min/max
- [logics.fj](hex/logics.fj) - xor, xor_zero, double_xor, or, and, not; the exact_xor family (double_/triple_/quadrupled_exact_xor); the sparse_* forms
- [input.fj](hex/input.fj) - input bits into hex, poll the keyboard, input ascii as hex, input decimal (signed & unsigned)
- [output.fj](hex/output.fj) - output hex as bits/bytes; print as hex-number (in ascii)
- [shifts.fj](hex/shifts.fj) - shl_bit/shr_bit (by 1 bit), shl_hex/shr_hex (by hexes)
- [math_basic.fj](hex/math_basic.fj) - inc/dec, neg, abs, count_bits, sign_extend
- [math.fj](hex/math.fj) - add/sub, {add/sub}_shifted, {add/sub}_constant, {add/sub}_hex_shifted_constant
- [mul.fj](hex/mul.fj) - add_mul, mul (works for signed & unsigned), mul10, mul_low, mul_const, fixed_mul{_low} (signed fixed-point)
- [div.fj](hex/div.fj) - div, idiv (signed), fixed_div (signed fixed-point)
- [strings.fj](hex/strings.fj) - the byte-buffer block helpers: input_ptr_line, print_ptr_text/line, fill_bytes, copy_bytes
- [tables_init.fj](hex/tables_init.fj) - hex.init: initializes the "results-tables" of the hex macros (the or, and, add, sub, cmp and mul inits)
- [pointers/](hex/pointers) - hex-vec pointers subdirectory: [flip](hex/pointers/xor_to_pointer.fj), [jump](hex/pointers/basic_pointers.fj), [xor_to](hex/pointers/xor_to_pointer.fj), [xor_from](hex/pointers/xor_from_pointer.fj); [stack](hex/pointers/stack.fj)/[pointers](hex/pointers/basic_pointers.fj) init. [pointer arithmetics](hex/pointers/pointer_arithmetics.fj), [stack arithmetics + push/pop](hex/pointers/stack.fj).


### [casting.fj](casting.fj)
Offers casting between bits and hexes: bit2hex, hex2bit.

### [screen.fj](screen.fj)
The screen device's commands (output_bits, init_screen, define_palette, select_palette, builtin_palette, set_max_fps, set_window_title, set_window_icon_header, draw_raw, draw_collines, end_frame, end_command, present), as the [I/O devices README](../interpreter/io_devices/README.md) specifies them.

### [ptrlib.fj](ptrlib.fj)
Init macros for pointers, stack and call/ret functions based on hex.pointers (ptr_init, stack_init, call, return, get_sp). Also offers fast call/ret macros (fcall, fret).

### [conf.json](conf.json)
A configurable json file that maintains ordered lists of the standard library files. 


# Documentation & Complexities
Every macro is documented with:
- Its **time complexity** (and if it's significantly different, the **space complexity** as well).
  - The complexities use the **@** sign, which is the log2 of the total number of fj ops in this program (can be it usually will be between 15-25).
  - The reason for this _weird_ log complexity, is that `wflip address, value` has the complexity of the number of on-bits in _value_ (usually an address, so about @/2); And it is widely used.
  - A chained step's wflip (`wflip .ret+w, prev_return ^ return`, see `hex.tables.chained_jump`) flips the xor of two nearby pad-16 return addresses, so it costs 2.3 on average (median 2, 90% at most 4), not @/2.
- An explanation about what the macro does (usually in a python-like syntax).
- The type of each of the parameters (bit/hex/bit.vec/hex.vec/constant/address and so on).
  - if a parameter is called bit/hex, it is a bit/hex variable (of size 1).
  - if a parameter (in a macro declared under the bit/hex namespaces) is used in the documentation as an array (x[:n], ascii[:8], etc.) - it is a bit/hex vector of the specified size; and that size is a size-constant.
- Optionally, what the macro assumes about the parameters, and what can you assume about the result.

## Fast search:
If you want to get to the source of a macro named some_macro (to view the macro's documentation, or its implementation), search in the stl/ folder for `'def some_macro '`.

The official [JetBrains](https://plugins.jetbrains.com/plugin/32134-flipjump/reviews) and [VS Code](https://marketplace.visualstudio.com/items?itemName=flipjump.flipjump) plugins let you jump to a macro's definition with Ctrl+Click on the macro name.


# The Standard Library Basics

## Reserved Names
The standard library reserves all the labels under the "stl", "bit", "hex", and "screen" namespaces.

No other labels are reserved or used by the standard library.


## Startup Macros
FlipJump programs start running at address 0. The IO opcode is placed at address 2w, and a normal program just skips above it and then starts.

To keep things simple, we wrote the ```stl.startup``` macro. It should be the first used macro/op in your program. If you are using multiple files, then **it should only be declared once**, at the start of the first file.

Basically, The first line in a FlipJump program should be ```stl.startup```.

Instead, you can also use the ```stl.startup_and_init_all``` macro, which does the startup, and also every ```init``` macro that's exist in the standard library.

## Padding
We decided to make the padding a part of the FlipJump assembly.

```pad n``` - A special assembly-op that fills the current address with arbitrary fj ops, until the address is divisible by (n*dw).

We made it a part of the FlipJump assembly, in spite of the fact that padding CAN be defined with the other primitives of the FlipJump assembly:
```c
// @note - padding can also be implemented in fj itself! (but the saved-word pad is more compile-time efficient)
//   pad zeros up to the address
def pad address @ pad_start {
  pad_start:
    rep((0-pad_start/(2*w))%address, i) fj 0, 0
}
```
By making the padding a part of the assembly, we can (and do) **utilize the free space** we just gained.

The free space, gained by the padding, is also **being used for storing fj-ops that will be created by future ```wflip```s**. 

That way, **using the ```pad n```** macros won't only **not waste up space**, but might even **save space**; That's because ```wflip```s to a padded address are smaller (less 1's in that address binary representation -> less fj-ops will be created for ```wflip```ing there).

# Contribute

The FlipJump philosophy is to be the simplest language of all, that can do any modern computation.

FlipJump should be below the OS, as it's a cpu-architecture after all.

The FlipJump stl should be minimalistic, efficient in both space and time, and offer macros similar to x86 ops.

The generic stl macro should look like `macro_name n dst src` for an n-bit/hex variable, with dst being the destination-variable, and src being the source-variable.
- e.g. the [hex/math.fj](hex/math.fj) / `hex.add n, dst, src`. 

For more information about contributions, see [I-Want-To-Contribute Thread](https://github.com/tomhea/flipjump/discussions/148) and [CONTRIBUTING.md](../../CONTRIBUTING.md).


# Read More

You can explore the full list of all macros from the [esolangs page](https://esolangs.org/wiki/FlipJump#The_Standard_Library).

If you are new to the FlipJump standard-library, Start by reading the [bit/logics.fj](bit/logics.fj) standard library file (start with `xor`). You can continue to the [bit.if](bit/cond_jumps.fj) macro afterwords. That's where the FlipJump magic begins.

If you want to understand how the deep optimized hex macros work, understand how the next macros are implemented: [hex.exact_xor](hex/logics.fj), [hex.output](hex/output.fj), [hex.inc1](hex/math_basic.fj), and [hex.add](hex/math.fj) (understand the concept of the [lookup tables](https://esolangs.org/wiki/FlipJump#Lookup_Tables).
