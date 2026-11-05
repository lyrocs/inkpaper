# Appendix A: Find a Rust idea by its job

A language feature is easier to remember when you remember what it made possible.
Use this index to return to the chapter where a Rust idea solves a concrete
problem. These are entry points, not a list of every syntax form that appears in
the source.

## Describe a screen and own its state

| Rust idea | The question it answers | Read |
|---|---|---|
| Cargo workspaces and features | How can a desktop app and firmware share code without sharing every dependency? | [1](ch01-workspace.md) |
| `core`, `alloc`, and `std` | Which services does this part of the program actually need? | [1](ch01-workspace.md) |
| Builders that consume `self` | How can a chain of calls construct a screen description? | [2](ch02-ui-language.md) |
| Traits and `impl Trait` | How can different element types participate in one UI? | [2](ch02-ui-language.md) |
| Generics and associated types | How can the type of a container express the children it holds? | [2](ch02-ui-language.md) |
| Typed handles and `PhantomData` | How can a temporary view refer to state that outlives it? | [3](ch03-entities.md) |
| Borrowing through a context | Who is allowed to read or update application state right now? | [3](ch03-entities.md) |
| `Result` versus panicking accessors | Is a failed state lookup recoverable or a broken program assumption? | [3](ch03-entities.md) |
| Type erasure and `unsafe` invariants | How can one store hold differently typed values without losing its safety contract? | [4](ch04-slot-table.md) |
| Alignment, initialization, and destruction | What must be true before bytes can be treated as a Rust value? | [4](ch04-slot-table.md) |
| Storage strategy types | How can each runtime choose bounded or heap-backed storage? | [4](ch04-slot-table.md) |
| Closures, `Fn`, and `'static` | How can a button keep behavior to call after rendering has finished? | [5](ch05-listeners.md) |

## Turn intent into pixels and interaction

| Rust idea | The question it answers | Read |
|---|---|---|
| Enums versus traits | When should the set of possibilities be closed, and when should it be extensible? | [6](ch06-frame-tree.md), [8](ch08-painting.md) |
| Index-based trees | How can a retained frame represent relationships without owning pointers between nodes? | [6](ch06-frame-tree.md) |
| Newtypes and integer arithmetic | How can layout calculations preserve units and handle pixel boundaries? | [7](ch07-layout.md) |
| Explicit enum variants | How is “unbounded space” different from a very large number? | [7](ch07-layout.md) |
| Associated error types and trait objects | How can one painting interface work with different backends? | [8](ch08-painting.md) |
| Bounded collections | What should happen when there are too many damaged rectangles to track? | [9](ch09-damage.md) |
| Typed events and marker traits | How can a component report intent without knowing its owner's implementation? | [10](ch10-events.md) |
| State machines | How do a press, a release, and a cancellation become different behaviors? | [10](ch10-events.md) |

## Work with devices without blocking the reader

| Rust idea | The question it answers | Read |
|---|---|---|
| Generic drivers and `embedded-hal` | How can a chip driver work with more than one microcontroller? | [11](ch11-i2c-driver.md) |
| Error enums and validated values | How do bus failures and invalid measurements stay distinguishable? | [11](ch11-i2c-driver.md) |
| Async traits and timeouts | How can software wait for a display without waiting forever? | [12](ch12-epaper.md) |
| Enum dispatch | How can firmware select among known display controllers at runtime? | [12](ch12-epaper.md) |
| Ownership of peripherals | How does Rust help control access to real hardware? | [13](ch13-firmware.md) |
| Futures, tasks, channels, and signals | How can several activities make progress while most are waiting? | [13](ch13-firmware.md) |
| Requests as data | How can a synchronous UI ask for asynchronous work? | [14](ch14-services.md) |
| Scoped borrowing around `.await` | How can a service release UI access while I/O is pending? | [14](ch14-services.md) |

## Make document bytes readable

| Rust idea | The question it answers | Read |
|---|---|---|
| Minimal I/O traits | How can an EPUB parser read from either a desktop file or embedded storage? | [15](ch15-epub.md) |
| Checked arithmetic and validated paths | How should a parser handle data it cannot trust? | [15](ch15-epub.md) |
| Borrowed resource views | Why can't a glyph cache be changed freely while its pixels are borrowed? | [16](ch16-fonts.md) |
| UTF-8 and source ranges | Why are bytes, characters, glyphs, and screen positions different coordinates? | [17](ch17-shaping.md) |
| Lifetimes at a pipeline boundary | Which pagination data is borrowed, and what can the reader retain? | [18](ch18-reader.md) |
| Semantic position types | How can a reading position survive a change in page layout? | [18](ch18-reader.md) |

## Make development clearer and measurable

| Rust idea | The question it answers | Read |
|---|---|---|
| Declarative and procedural macros | When does generated Rust make UI code easier to understand? | [19](ch19-macros.md) |
| Parsing tokens and producing diagnostics | How can a custom notation fail helpfully rather than mysteriously? | [19](ch19-macros.md) |
| `Drop` guards | How can a timed span end on an early return as well as the normal path? | [20](ch20-measuring.md) |
| Conditional compilation and instrumentation | How can measurement be available when needed without assuming it is free? | [20](ch20-measuring.md) |

If a chapter feels difficult, follow its product question first. You can often
understand why a boundary exists before understanding every generic parameter in
its implementation. Return to the signature after the behavior makes sense.
