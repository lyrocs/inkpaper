# Appendix A: Rust concept index

Use this table to find where a Rust feature is explained with an example from
InkPaper. Chapter numbers point to the main explanation, and later chapters
often revisit the idea.

## Language basics

| Concept | Chapters |
|---------|----------|
| Newtypes (`struct Pixels(i32)`) | 2, 7, 11, 15 |
| Enums with data; closed sets of variants | 2, 6, 9, 12 |
| `match`: tuples, guards, or-patterns, struct patterns with literals | 4, 5, 6, 11, 17 |
| Slice patterns (`[]`, `[x]`, `[_, y, ..]`, `let &[a, b] = ... else`) | 18, 19 |
| `let ... else` | 5, 7, 9, 12 |
| `if let` / `while let`, let chains | 1, 6, 10 |
| `if` and `match` as expressions | 9, 13 |
| The never type `!`, `return` in match arms | 10, 13 |
| `const fn`, `const` items, inline `const { }` blocks | 2, 9, 10, 17 |
| Integer overflow: checked, saturating, wrapping | 4, 7, 15, 16 |
| Lossless `From` vs fallible `TryFrom`; `as` casts and their lints | 1, 7, 16 |
| Operator overloading (`Add`, `Sub`, `Mul<Rhs>`) | 7 |
| Strings: UTF-8 bytes, `char`, byte slicing, `str::get` | 6, 15, 17, 18 |

## Ownership, borrowing and lifetimes

| Concept | Chapters |
|---------|----------|
| `self` by value for builders and one-shot types | 2, 14 |
| Lifetimes in structs (`Text<'a>`, `Context<'a, T>`) | 2, 3 |
| Outputs that borrow inputs (`Pagination<'a>`) | 18 |
| Several lifetimes on one struct | 18 |
| Borrowed results that block mutation (`GlyphBitmap<'_>`) | 16 |
| `'static` bounds and why stored closures need them | 3, 5 |
| `Cow` and `into_owned` | 18 |
| Ownership of hardware (peripherals, buses, pins) | 11, 13 |
| Never holding a borrow across `.await` | 14 |
| Drop guards; `let _ = x` vs `let _guard = x` | 3, 4, 10, 20 |

## Traits and generics

| Concept | Chapters |
|---------|----------|
| Default methods, supertraits | 2, 16 |
| Associated types; bounds and equality constraints on them | 2, 8, 14 |
| Generic associated types (`type WithChild<E>`) | 2, 4 |
| Blanket implementations | 2 |
| Marker traits (`Global`, `EventEmitter<E>`) | 3, 10 |
| `impl Trait` in argument and return position | 2, 3 |
| Trait objects (`dyn`) vs generics | 6, 8, 13 |
| Implementing a trait for `()` | 8 |
| Strategy types for configuration (`RuntimeStorage`) | 4 |
| Const generics, with defaults | 4, 9, 16 |
| `PhantomData` and variance | 3, 5 |
| Manual `Copy`/`Clone` impls to avoid derive bounds | 3 |
| Derived ordering (enums by declaration order, structs lexicographically) | 9, 15, 18 |
| `async fn` in traits | 12, 14, 15 |

## Closures and iterators

| Concept | Chapters |
|---------|----------|
| `Fn`, `FnMut`, `FnOnce` | 3, 5 |
| Methods and enum variants as functions | 5, 11, 14 |
| Higher-ranked bounds (`for<'a>`) | 5 |
| Visitor closures instead of collections | 16, 17 |
| `Option` combinators (`map`, `and_then`, `or_else`, `filter`, `is_some_and`, `is_none_or`, `then_some`) | 5, 7, 10, 14, 15 |
| Iterator adapters (`rev`, `fold`, `scan`, `flatten`, `copied`, `filter`) | 9, 13, 15, 18 |
| `partition_point`, `rotate_left`, slice comparison | 12, 14, 18 |

## Unsafe and low-level

| Concept | Chapters |
|---------|----------|
| `unsafe`, `# Safety` docs, `// SAFETY:` comments, module invariants | 4 |
| Raw pointers, `NonNull`, `addr_of_mut!`, `drop_in_place` | 4 |
| Type erasure with `TypeId`, `Layout` and function pointers | 4, 5 |
| `MaybeUninit`, `UnsafeCell`, `Cell`, `RefCell` | 4 |
| `#[repr(C)]`, `#[repr(u8)]`, alignment, zero-sized types | 4, 11 |
| Panic safety | 4 |

## Embedded and async

| Concept | Chapters |
|---------|----------|
| `no_std`, `core` vs `alloc` vs `std`, `#![no_main]` | 1, 13 |
| `embedded-hal` traits, generic drivers | 11, 12 |
| `async`/`await`, futures, executors | 11, 13 |
| `select`, timeouts, cancellation by drop | 12, 13 |
| Pinning (`Pin`, `pin!`) | 13 |
| Tasks, `Channel`, `Signal`, shared buses, `StaticCell` | 13 |
| Request/reply over channels with `Arc` | 14 |
| Critical sections, atomics, `target_has_atomic` | 13, 20 |
| Floats in `no_std` with `libm` | 16 |
| `defmt` logging | 13 |

## Errors

| Concept | Chapters |
|---------|----------|
| Error enums and `From` for `?` | 6, 15 |
| Generic error wrappers (`Error<E>`, `PaintError<E>`) | 8, 11, 12, 15 |
| `panic!` vs `Result`, `try_` variants, `#[track_caller]` | 3 |
| `expect` with a reason | 15 |
| Reporting the root cause when cleanup also fails | 12 |
| `Result<Option<T>, E>` | 14 |

## Macros

| Concept | Chapters |
|---------|----------|
| `macro_rules!`: fragments, repetitions, `$crate`, `stringify!` | 2, 19, 20 |
| The macro callback pattern | 19 |
| Procedural macros with `syn`, `quote`, `proc-macro2` | 19, 20 |
| Spanned errors, hygiene (`Span::mixed_site`) | 19, 20 |
| Compile-fail tests with `trybuild` | 19 |
| Derive macros from crates (`bitfield`, `minicbor`, `clap`) | 11, 14, 20 |

## Tooling and project structure

| Concept | Chapters |
|---------|----------|
| Workspaces, `default-members`, workspace dependencies, profiles | 1 |
| Features, optional dependencies, additivity | 1, 8, 16, 20 |
| Conditional compilation (`cfg`, `cfg_attr`) | 1, 2, 16 |
| Lints, `#[expect]` vs `#[allow]` | 1, 8, 16 |
| Toolchains, targets, `build-std`, runners | 1 |
| The xtask pattern, `justfile` | 1, 15, 20 |
| Benchmarks and metrics before optimising | 7, 9, 20 |
