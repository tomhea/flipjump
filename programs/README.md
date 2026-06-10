# `programs/` — FlipJump example & test programs

This directory holds FlipJump (`.fj`) programs: a few standalone demos plus the
program files used as inputs by the test suite. Most subdirectories are themed
collections of small programs that each exercise a particular language feature
or standard-library macro; they are compiled and run by the tests defined in
[`tests/tests_tables/`](../tests/tests_tables) (see [`tests/README.md`](../tests/README.md)).

Every `.fj` file begins with a `// ...` comment describing what it does, and
every subdirectory has its own README with a per-file table.

> The [`catalog/`](catalog) directory has its own
> [`README.md`](catalog/README.md) and is not covered here.

## Standalone programs

| File | Purpose |
|---|---|
| `calc.fj` | A calculator for the `+ - * / %` operations on two decimal/hexadecimal numbers. |
| `prime_sieve.fj` | Prints all the primes up to an input number `n` (decimal). |
| `quine16.fj` | A 16-bit quine — prints its own compiled program. |

## Subdirectories

| Directory | Purpose |
|---|---|
| [`print_tests/`](print_tests) | Input/output and number-printing programs (hello-world, cat, decimal/hex printing). |
| [`sanity_checks/`](sanity_checks) | The smallest, fastest single-macro/feature sanity tests. |
| [`simple_math_checks/`](simple_math_checks) | Small binary (`bit.*`) math checks: add, compare, divide, shift. |
| [`func_tests/`](func_tests) | Function-call / call-stack semantics (`stl.call`, push/pop). |
| [`concept_checks/`](concept_checks) | Language-concept demos: pointers, casting, namespaces-as-classes, segments. |
| [`pair_ns_tests/`](pair_ns_tests) | Tests for the `Pair` "class" defined in `concept_checks/pair_ns.fj`. |
| [`multi_comp/`](multi_comp) | Multi-file compilation tests (several files, several orders, with/without the STL). |
| [`sorts/`](sorts) | Bubble-sort programs that exercise the memory-access/pointer macros. |
| [`hexlib_tests/`](hexlib_tests) | The large `hex.*` standard-library test suite (basics, shifts, two-operand ops, div, mul). |
