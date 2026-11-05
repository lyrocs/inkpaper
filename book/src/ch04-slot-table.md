# 4. Making memory predictable: the slot table

A reader should not run out of UI memory simply because someone opens the same menu many times. Nor should adding a battery indicator silently invalidate a pointer to the book state. These expectations sound ordinary until the runtime must store values of different types, allow nested access, and optionally work without a general-purpose heap.

Chapter 3 gave application code a small typed handle. This chapter looks behind it. The objective is not to learn every raw-pointer operation in the repository. It is to understand the handful of promises that let a safe handle API sit on top of heterogeneous storage—and why those promises matter to the person holding the reader.

## Two budgets, not one

A table of entity state has to accommodate both *how many values* exist and *how much space they occupy*. Thirty tiny values can exhaust a slot limit; one large value can exhaust the byte buffer. Increasing one limit does not fix the other. Alignment adds padding, and metadata consumes memory outside the value buffer, so a byte capacity is not the size of the entire runtime.

The common mechanism is `SlotTable<S, M>` in `crates/ui/src/slot_table.rs`. `S` selects the storage; `M` is metadata chosen by the entity, callback, or global arena. This is its exact slot definition:

```rust,ignore
struct Slot<P, M> {
    place: P,
    meta: M,
    drop_fn: unsafe fn(*mut u8),
    state: SlotState,
    borrow: BorrowState,
}
```

The missing type parameter is the interesting one: there is no stored-value type `T`. A single table can therefore hold a battery state, a drawer state, and an application root. The slot remembers where the value lives and how to destroy it, rather than embedding every possible application type in one giant enum.

When the entity arena reserves a value, it supplies `Layout::new::<T>()`, `TypeId::of::<T>()`, and `drop_value::<T>`. The layout describes size and alignment; the type ID supports checking on access; the specialized function knows the correct destructor. The compiler generates that function for the concrete `T`, but the table stores it as an erased function pointer.

This is *type erasure*: retain the operations needed to manage a value while hiding its concrete type from the container. A `Box<dyn Any>` would provide a familiar heap-backed alternative. An application-specific enum could keep everything in safe Rust at the cost of knowing every variant and sizing each entry for the largest one. InkPaper's reusable runtime chooses a lower-level representation so it can accept arbitrary state types in fixed or heap storage.

## A place must fit before it becomes a value

The fixed implementation stores bytes inline. These exact declarations from `slot_table.rs` establish the buffer:

```rust,ignore
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

`const BYTES: usize` is a const generic: capacity participates in the type and determines the array's size. Inline does not necessarily mean stack-resident; it means the storage is part of its owning value, wherever that owner lives. `MaybeUninit` permits storage that has not yet been initialized. It avoids pretending that merely reserving space has constructed a valid application object.

`UnsafeCell` permits interior mutation through a shared reference. It does not make arbitrary mutation safe; it gives the implementation the primitive needed to enforce its own rules. The cursor uses the safer `Cell<usize>`, which allows replacing a small value without lending a reference to its contents.

The allocation strategy is a bump cursor. Align the next offset, reserve enough bytes, and advance. This exact method is the heart of `FixedMemory`:

```rust,ignore
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
```

The fixed buffer supports alignment up to 16. A more strongly aligned type is rejected rather than placed incorrectly. Checked arithmetic turns overflow into failure; exceeding the byte budget does the same. Even a zero-sized value consumes a one-byte place so reservations have distinct addresses.

This is intentionally not a full allocator. Removing a fixed-memory value does not move the cursor backward. A removed place can be reused when a later value fits its size and alignment; otherwise reserving new space can leave unusable holes. A complete reset reclaims the buffer. Heap storage instead gives each value its own allocation and can release that allocation when the value is removed. The metadata vector may grow without moving those separately allocated values.

For fixed storage, the safety promise is that live values do not move relative to their buffer and remain at valid addresses during outstanding borrows. It is not a claim that the outer runtime is magically pinned for its entire life. Rust's surrounding borrows prevent moving it while references into it are in use.

## The unsafe part is a collection of obligations

An `unsafe` block does not switch off Rust's type system. It permits operations whose prerequisites the compiler cannot prove, such as turning a raw pointer into a reference. Before doing that here, the implementation must know that memory is aligned, initialized, still alive, of the expected type, and borrowed compatibly.

The lifecycle records make the initialization promise concrete. Reservation creates an `Initializing` slot. Only after a valid value is written does the internal `commit` operation mark it `Live`. An abandoned construction never becomes readable and must not have a destructor run as though it contained a finished value.

Access adds another check. The entity arena validates the handle, looks up a live slot, checks its `TypeId`, and acquires the required borrow. The exact acquisition logic in `slot_table.rs` expresses the familiar rule:

```rust,ignore
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

This is an excerpt of the implementation with its separate `release` method omitted. Several readers can coexist; a writer requires a free slot. The guard on the shared count prevents overflow. Failure becomes a borrow conflict, not a second conflicting reference.

`RawEntityBorrow` pairs a successful acquisition with release in its `Drop` implementation. The safe `read` and `update` APIs keep that guard alive while the user closure runs. Only within that interval do they create typed references from the validated pointer. The closure boundary from chapter 3 is thus part of the safety argument, not merely an API preference.

Two less visible rules deserve equal attention. The slot-list `RefCell` borrow is released before user code runs, so a handler can create another entity without re-borrowing the whole list illegally. And the fixed implementation obtains raw pointers without first creating a mutable reference to the entire byte buffer. Such a whole-buffer reference would overlap references to live values inside it, violating the very exclusivity being enforced per slot.

Destruction has its own ordering. Removal marks a slot removed *before* invoking its destructor, preventing a destructor that unwinds from leaving the slot apparently live and eligible for a second destruction. The table traverses slots in reverse order when removing or clearing them. With callback slot reuse, that table order should not be confused with a fresh chronological ordering of every value ever placed there.

These obligations work together. A type check alone cannot prove a pointer is live. A live slot alone cannot prove exclusive access. Reviewing unsafe code means following the whole chain, not being reassured by the presence of a `SAFETY` comment.

## Choose the budget at the runtime boundary

Applications select storage through `RuntimeStorage`. This complete pedagogical declaration uses the public API and the same configuration pattern documented in `crates/ui/src/storage.rs`:

```rust,noplayground
use inkpaper_ui::*;

struct DeviceStorage;

impl RuntimeStorage for DeviceStorage {
    type Entities = FixedEntityArena<4_096, 8>;
    type Callbacks = FixedCallbackArena<2_048, 16>;
    type Globals = FixedGlobalArena<256, 4>;
    type Frame = FixedFrame<96, 2_048>;
    type ElementStates = FixedElementStates<32>;
    type Events = FixedEvents<8>;
}
```

It declares a storage policy, not a complete reader application; these capacities are not a promise that `InkPaperApp` fits. Associated types let each runtime table have an appropriate representation. With `alloc` enabled, heap-backed choices become available and may be mixed with fixed ones. The feature supplies a capability; the type selects how a particular runtime uses it.

Fixed tables make exhaustion explicit and predictable. They do not bound allocations performed *inside* stored values: an entity containing a growing `Vec` still uses the heap. Heap tables relieve manual capacity planning but bring allocator failure and allocation overhead, and slot identifiers still have representational limits. Neither option is infinite memory. The useful question is which resources the application needs to bound and where failure can be handled sensibly.

## Try it: spend a tiny memory budget

Optional, read-only: simulate fresh reservations in `FixedEntityArena<64, 4>`, using the `reserve` and `place` methods. Four `u64` values use 32 payload bytes with their usual eight-byte alignment; a fifth reservation fails on slots before consuming more payload space. Now start fresh with `[u8; 40]`: the first fits, but the second exceeds 64 bytes, despite an available slot.

The observable result of this code trace is two different error paths: `SlotsFull` and `StorageFull`. Add an imagined type aligned to 32 and find the third path, `UnsupportedAlignment`. No unsafe edits or hardware are needed. You are practicing the same budget reasoning that makes a repeatedly opened menu dependable.
