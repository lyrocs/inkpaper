# 4. An allocator-free arena

> **Where we are:** commit `496fe14`, "no alloc entity arena", 8 August 2026,
> where a 534-line `arena.rs` appeared. We then jump to its descendants:
> `2b01497`–`a5f4fda` (9 September, optional heap storage) and the refactor of 4
> October (`f48dc17`–`a9f8678`), which merged three arenas into one **slot table**
> and let applications choose storage with a type.

## The requirements

Chapter 3 said the runtime owns every entity's state. Now we have to build the
place where it lives. Here is what it must do:

1. Store values of **many different types**: an `InkPaperApp`, a `BatteryState`,
   a `ReaderDrawer`... all in one container.
2. Work **without a heap**, in a buffer whose size is fixed at compile time.
3. Never **move** a value once stored, because code may hold a pointer to it while
   it runs.
4. Check at runtime that nobody reads an entity while someone else is mutating
   it, the job `RefCell` normally does.
5. **Drop** every value exactly once, with its correct destructor.

Safe Rust can't express this directly. A `Vec<T>` holds one type, and a
`Vec<Box<dyn Any>>` allocates. So this is the chapter where InkPaper reaches for
`unsafe`. The interesting part is how carefully it does so.

## What `unsafe` actually means

`unsafe` does not turn off the borrow checker. It unlocks five extra
abilities, and the main one here is **dereferencing raw pointers**
(`*const T` / `*mut T`). The compiler can't verify those operations, so you
promise that they are correct. The Rust convention is to write the promises
down:

- Every `unsafe fn` gets a `# Safety` section saying what the caller must
  guarantee.
- Every `unsafe { ... }` block gets a `// SAFETY:` comment saying why the
  guarantee holds here.

InkPaper goes one step further. The module that contains the unsafe code begins
with its **invariants**, rules the whole module keeps:

```rust
//! SAFETY INVARIANTS:
//!
//! 1. a place is never reused while its slot exists, so values never move and never
//!    overlap. Growing the slot list moves slots, not values.
//! 2. only `Live` slots hold an initialized value. Each is dropped exactly once: when it
//!    is removed, or when the table is cleared or dropped, in reverse reservation order.
//! 3. no `RefCell` guard on the slot list is held while user code runs, so callbacks may
//!    reserve new slots.
//! 4. no reference to a fixed byte buffer as a whole is created while references to
//!    stored values may exist.
//! 5. borrow states only count borrows. Arenas pair each `acquire` with one `release`
//!    and convert pointers into references only while the matching borrow is held.
```

If you remember one thing from this chapter, let it be this habit. Unsafe code is
correct only relative to rules like these. Writing them down lets a reviewer
check each `unsafe` block against a short list instead of against the whole
program.

## Type erasure: one table, many types

The table is `SlotTable<S, M>` in `crates/ui/src/slot_table.rs`. Each entry is a
`Slot`:

```rust
struct Slot<P, M> {
    place: P,
    meta: M,
    drop_fn: unsafe fn(*mut u8),
    state: SlotState,
    borrow: BorrowState,
}
```

There is no `T` anywhere. The table stores *bytes*, and each slot remembers
three things about the value in them:

- **`place`**: where the bytes are.
- **`meta`**: arena-specific metadata. For entities it holds the value's
  `TypeId`, so every access can check the type.
- **`drop_fn`**: a **function pointer** to the value's destructor.

The destructor comes from a tiny generic function:

```rust
pub(crate) unsafe fn drop_value<T>(ptr: *mut u8) {
    unsafe { core::ptr::drop_in_place(ptr.cast::<T>()) };
}
```

When you write `drop_value::<BatteryState>`, the compiler generates a version of
this function specialised for `BatteryState`, a process called
**monomorphisation**. The result coerces to a plain `unsafe fn(*mut u8)` that has
forgotten the type but still knows how to drop it. This is **type erasure by hand**:
we keep a *function* that knows the type, while the data itself is untyped bytes.
`Box<dyn Trait>` does the same thing behind the scenes with a vtable.

Recall `create_entity` from chapter 3:

```rust
let reservation = store.reserve(Layout::new::<T>(), TypeId::of::<T>(), drop_value::<T>)?;
```

- `Layout::new::<T>()` gives the size and alignment of `T`.
- `TypeId::of::<T>()` gives a unique id for the type. It needs `T: 'static`,
  which is one reason `Render` requires `'static`.
- `drop_value::<T>` gives the destructor.

Those three values are all the table needs to manage a `T` without knowing what
a `T` is.

## Where the bytes live: a bump allocator

Where values live is a pluggable strategy, chosen by the `ValueMemory` trait. The
allocator-free version is a **bump allocator** over an inline buffer:

```rust
#[repr(C, align(16))]
struct Bytes<const N: usize> {
    bytes: [core::mem::MaybeUninit<u8>; N],
}

/// bump allocation in an inline buffer of `BYTES` bytes, aligned to 16
pub struct FixedMemory<const BYTES: usize> {
    storage: core::cell::UnsafeCell<Bytes<BYTES>>,
    cursor: core::cell::Cell<usize>,
}
```

Several Rust features meet here.

**Const generics.** `const BYTES: usize` is a *value* used as a type parameter.
`FixedMemory<4096>` and `FixedMemory<8192>` are different types with different
sizes, and the array length `N` comes straight from the parameter. No heap is
needed, because the size is known at compile time.

**`MaybeUninit<u8>`.** A plain `[u8; N]` would have to be initialised and
would promise every byte is a valid `u8`. `MaybeUninit` means "memory that may
not hold a valid value yet", which is exactly what reserved-but-unwritten space
is.

**`#[repr(C, align(16))]`.** By default Rust may lay out a struct however it
likes. `repr(C)` fixes the layout, and `align(16)` puts the buffer on a 16-byte
boundary. Each placement then only has to align its *offset*.

**`UnsafeCell` and `Cell`.** We need to write into the buffer through a shared
reference (`&self`), because many entities live in one table and the table is
shared. Mutating through `&` is normally forbidden. `UnsafeCell` is the *only*
legal way to opt out, and every interior-mutability type (`Cell`, `RefCell`,
`Mutex`) is built on it. `Cell<usize>` is the safe version for small `Copy`
values: you `get()` and `set()`, but you never get a reference inside.

Placing a value just moves the cursor forward:

```rust
fn place(&self, layout: Layout) -> Result<FixedPlace, ReserveError> {
    let alignment = layout.align();
    if alignment > FIXED_ALIGNMENT {
        return Err(ReserveError::UnsupportedAlignment {
            requested: alignment,
            supported: FIXED_ALIGNMENT,
        });
    }

    let offset = align_up(self.cursor.get(), alignment).ok_or(ReserveError::StorageFull)?;
    let size = layout.size().max(1);
    let end = offset.checked_add(size).ok_or(ReserveError::StorageFull)?;
    if end > BYTES {
        return Err(ReserveError::StorageFull);
    }

    self.cursor.set(end);

    Ok(FixedPlace { offset, size })
}

fn align_up(value: usize, alignment: usize) -> Option<usize> {
    debug_assert!(alignment.is_power_of_two());

    let mask = alignment - 1;
    value.checked_add(mask).map(|v| v & !mask)
}
```

All the arithmetic is **checked**: `checked_add` returns `None` on overflow
instead of wrapping, and `ok_or` turns that `None` into an error. When the buffer
is full you get `Err(StorageFull)`, never a corrupted neighbour.

`size.max(1)` is a subtle detail. **Zero-sized types** (like the unit structs from
chapter 2) occupy no bytes, so two of them could share an address. Bumping by at
least one byte gives each value its own address, and the table's bookkeeping
relies on that.

Getting a pointer to a place has to respect invariant 4:

```rust
fn base(&self) -> *mut u8 {
    let storage = self.storage.get();

    // SAFETY: creates a raw pointer to the buffer without referencing it as a whole
    unsafe { core::ptr::addr_of_mut!((*storage).bytes).cast::<u8>() }
}
```

Why not just write `&mut (*storage).bytes`? Because that would create a
`&mut` to the *whole buffer* while other code might hold `&T` references to
values inside it. Two such references overlapping is undefined behaviour, even if
nobody writes through them. `addr_of_mut!` makes a raw pointer *without* creating
an intermediate reference. This is the kind of detail that Miri, Rust's
undefined-behaviour detector, is built to catch.

### The heap version

With the `alloc` feature there is a second implementation, `HeapMemory`, which
gives each value its own allocation:

```rust
#[cfg(feature = "alloc")]
pub struct HeapPlace(Option<(NonNull<u8>, Layout)>);

#[cfg(feature = "alloc")]
impl Drop for HeapPlace {
    fn drop(&mut self) {
        if let Some((ptr, layout)) = self.0.take() {
            // SAFETY: this place owns the allocation and its original layout
            unsafe { alloc::alloc::dealloc(ptr.as_ptr(), layout) };
        }
    }
}
```

`NonNull<u8>` is a raw pointer that is guaranteed not to be null, so
`Option<NonNull<u8>>` has the same size as a bare pointer (the *niche
optimisation*). `Option::take()` swaps the value out and leaves `None`, so even a
second `drop` couldn't free twice.

## A hand-written `RefCell`

Invariant 5 says borrow states only *count* borrows. Here is the counter:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum BorrowState {
    Free,
    Shared(u16),
    Exclusive,
}

impl BorrowState {
    fn acquire(self, kind: BorrowKind) -> Option<Self> {
        match (kind, self) {
            (BorrowKind::Shared, Self::Free) => Some(Self::Shared(1)),
            (BorrowKind::Shared, Self::Shared(count)) if count < u16::MAX => {
                Some(Self::Shared(count + 1))
            }
            (BorrowKind::Exclusive, Self::Free) => Some(Self::Exclusive),
            _ => None,
        }
    }
}
```

That is Rust's borrowing rule ("many readers *or* one writer") written as a
`match` on a tuple. Matching on `(kind, self)` lets each line state one whole
case, and the `_ => None` arm catches every conflict. The `if count < u16::MAX`
**match guard** stops the counter from overflowing.

The entity arena puts it all together when someone asks for a borrow:

```rust
fn borrow(
    &self,
    entity: EntityId,
    type_id: TypeId,
    kind: BorrowKind,
) -> Result<NonNull<u8>, EntityAccessError> {
    if entity.generation() != 0 {
        return Err(EntityAccessError::InvalidEntity);
    }

    let slot = usize::from(entity.slot());
    let (meta, ptr) = self.slots.live(slot).map_err(|error| match error {
        NotLive::Initializing => EntityAccessError::NotReady,
        NotLive::Missing | NotLive::Abandoned | NotLive::Removed => {
            EntityAccessError::InvalidEntity
        }
    })?;

    if meta.type_id != type_id {
        return Err(EntityAccessError::TypeMismatch);
    }

    self.slots
        .acquire(slot, kind)
        .map_err(|_| EntityAccessError::BorrowConflict)?;

    Ok(ptr)
}
```

It checks, in order, that the handle is valid, that the slot holds a live value,
that the type matches, and that the borrow doesn't conflict. Only after all four
does it hand out a pointer. A **guard** pairs every successful `acquire` with a
`release`:

```rust
impl Drop for RawEntityBorrow<'_> {
    fn drop(&mut self) {
        self.store.release(self.entity, self.kind);
    }
}
```

Now the `unsafe` in `Entity::try_read` from chapter 3 makes sense:

```rust
let borrow = RawEntityBorrow::acquire(
    cx.runtime.entities,
    self.id,
    TypeId::of::<T>(),
    BorrowKind::Shared,
)?;

let value = unsafe { &*borrow.ptr().cast::<T>().as_ptr() };

Ok(f(value))
```

The cast to `*const T` is justified because the type was checked. The `&*`
creates a shared reference, which is justified because a shared borrow is
registered. And `borrow` lives until the end of the function, *after* `f`
returns, so the reference never outlives the registration. The closure-based API
from chapter 3 is what makes this provable: the reference can't escape `f`.

Invariant 3 matters here too. The table keeps its slot list in a `RefCell`, but
it never holds that `RefCell` borrowed while user code runs. Otherwise an entity
that creates another entity during `update` would hit "already borrowed" and
panic.

## Dropping exactly once

Removal is careful about the order of operations:

```rust
for slot in slots.iter_mut().rev() {
    if slot.state != SlotState::Live || !predicate(&slot.meta) {
        continue;
    }

    // mark first, so an unwinding destructor can't lead to a second drop
    slot.state = SlotState::Removed;
    removed += 1;

    // SAFETY: live slots hold an initialized value matching `drop_fn`
    unsafe { (slot.drop_fn)(self.memory.ptr(&slot.place).as_ptr()) };
    self.memory.free(&mut slot.place);
}
```

A user's destructor might panic. If the state were updated *after* calling it,
an unwinding panic would leave the slot marked `Live`, and the table's own `Drop`
would run the destructor a second time: a double free. Marking first closes that
hole. `.rev()` drops values in reverse creation order, like local variables in a
function, so a value created later (which may refer to an earlier one) goes first.

## Choosing storage with a type

For its first month, the arena was purely fixed-size. In September a heap version
appeared behind the `alloc` feature, because the simulator had no reason to count
bytes. In October the two were unified. Today one `SlotTable` serves entities,
callbacks and globals, and **where its memory comes from is a type parameter**:

```rust
/// where a slot table keeps its values and slots
pub trait SlotStorage {
    type Memory: ValueMemory;
    type Slots<T>: VecStorage<T>;
}

/// at most `SLOTS` values in an inline buffer of `BYTES` bytes. Never allocates.
pub struct FixedSlots<const BYTES: usize, const SLOTS: usize>;

impl<const BYTES: usize, const SLOTS: usize> SlotStorage for FixedSlots<BYTES, SLOTS> {
    type Memory = FixedMemory<BYTES>;
    type Slots<T> = heapless::Vec<T, SLOTS>;
}
```

`FixedSlots` is a struct with no fields. It exists only to *name a choice*. Types
used this way are often called **type-level configuration** or "strategy
types". `type Slots<T>` is a generic associated type again: "a vector of any `T`".
For fixed storage it's a `heapless::Vec`, a vector with inline, compile-time
capacity. For heap storage it's a thin wrapper around `alloc::vec::Vec`. Both
implement the crate's own `VecStorage<T>` trait:

```rust
pub trait VecStorage<T>: Default + Deref<Target = [T]> + DerefMut {
    fn capacity(&self) -> usize;
    fn try_reserve(&mut self, additional: usize) -> Result<(), StorageError>;
    fn try_push(&mut self, value: T) -> Result<(), StorageError>;
    // ...
}
```

The supertrait `Deref<Target = [T]>` means "this derefs to a slice". Every slice
method (`iter`, `len`, indexing, `binary_search`...) then works on both kinds of
vector for free. The trait only adds what slices lack: fallible growth.

At the top sits `RuntimeStorage`, a trait whose associated types pick one storage
per table:

```rust
pub trait RuntimeStorage {
    type Entities: EntityStorage;
    type Callbacks: CallbackStorage;
    type Globals: GlobalStorage;
    type Frame: FrameStorage;
    type ElementStates: ElementStateStorage;
    type Events: EventStorage;
}
```

The simulator chooses heap tables with initial reservations:

```rust
impl RuntimeStorage for SimulatorStorage {
    type Entities = HeapEntityArena<32>;
    type Callbacks = HeapCallbackArena<64>;
    type Globals = HeapGlobalArena<8>;
    type Frame = HeapFrame<64, 2_048>;
    type ElementStates = HeapElementStates<256>;
    type Events = HeapEvents;
}
```

and, built without `alloc`, the very same simulator uses fixed tables:

```rust
#[cfg(not(feature = "alloc"))]
pub(crate) type SimulatorStorage =
    FixedStorage<16_384, 32, 8_192, 64, 2_048, 32_768, 256, 2_048, 8, 8, 8>;
```

`FixedStorage` has **eleven const generic parameters**, several with defaults
(`const GLOBAL_BYTES: usize = 0`). It is a mouthful, but every byte the UI can
use is visible in one line.

Commit `f230425`, "choose runtime storage with a type instead of the alloc
feature", explains why this design beat the old one. With a feature flag, every
crate in the build had to agree: if *any* crate turned on `alloc`, everyone got
heap storage. With a type, each `Runtime<S>` picks its own storage, both kinds
can exist side by side in one program, and the tests can run the same code
against both. That's what the `justfile`'s double test run from chapter 1 does,
through this alias:

```rust
// unit tests run against the storage the `alloc` feature implies
#[cfg(all(test, not(feature = "alloc")))]
pub(crate) type TestEntityArena<const BYTES: usize, const SLOTS: usize> =
    FixedEntityArena<BYTES, SLOTS>;
#[cfg(all(test, feature = "alloc"))]
pub(crate) type TestEntityArena<const BYTES: usize, const SLOTS: usize> = HeapEntityArena<SLOTS>;
```

## Rust concepts in this chapter

- **`unsafe`**: what it allows, `# Safety` docs, `// SAFETY:` comments, and
  module-level invariants.
- **Raw pointers**, `NonNull`, `addr_of_mut!`, `ptr::drop_in_place`, and casting
  pointers between types.
- **Type erasure** with `TypeId`, `Layout` and monomorphised function pointers.
- **Const generics** (`FixedMemory<const BYTES: usize>`), including defaults.
- **`MaybeUninit`**, **`UnsafeCell`**, **`Cell`**, **`RefCell`**: the interior
  mutability family.
- **`#[repr(C, align(N))]`**, alignment, zero-sized types.
- **Checked arithmetic**: `checked_add`, `ok_or`, `?`.
- **Matching on tuples** and **match guards**.
- **Panic safety**: updating state before calling user code that may unwind.
- **Strategy types** and traits with associated types for configuration;
  supertraits like `Deref<Target = [T]>`.

## Try it yourself

1. Install Miri (`rustup +nightly component add miri`) and run
   `cargo +nightly miri test -p inkpaper-ui slot_table`. Then change
   `addr_of_mut!((*storage).bytes)` to `&mut (*storage).bytes as *mut _` and look
   for new warnings.
2. In a test, create a `FixedEntityArena<64, 4>` (it's a test-only API, see
   `entity/arena.rs`) and insert values until you get `StorageFull` or
   `SlotsFull`. Predict which comes first for `u64` values, then for `[u8; 40]`.
3. Write a type with a `Drop` impl that panics, insert it, and remove it inside
   `std::panic::catch_unwind`. Use a counter to confirm the destructor ran only
   once.
