# 9. Drawing less: damage and invalidation

> **Where we are:** a burst of work from 14 to 17 August 2026 (`fe9a2fd`
> "add rendering performance harness" through `1d215f6` "normalize damage for
> display refresh constraints"), then the October rework that made rebuilds
> partial (`eb84e0d`, `452e5b7`, `4018eae`, `136a64f`, 4 October), and the
> firmware's refresh policy (`6539d9a`, 2 September).

## Why e-paper changes everything

On a phone, redrawing the whole screen 60 times a second is normal. On e-paper,
a full refresh takes hundreds of milliseconds, flashes the screen black and
white, and uses noticeably more battery. A *partial* refresh of a small window
is faster and doesn't flash. The best refresh of all is the one you don't do.

So the framework must answer two questions after every change:

1. **What kind of work** does this change need? Nothing? Repaint only? New
   layout? A whole rebuild?
2. **Where** on the screen did anything change?

The answer to the first is an **invalidation**, and the answer to the second is
**damage**.

## Measure first

The first commits of this phase didn't optimise anything. They added a
performance harness (`fe9a2fd`), metrics (`10a1fb3`) and scenarios (`da1d5ff`).
`crates/ui/benches/` still contains files like `damage_metrics.rs` and
`partial_paint_metrics.rs`, which count exactly how many nodes were visited and
how many pixels were painted in typical situations. Only *after* those existed did
the optimisation commits start. Each one could then show a number going down.
Keep that order in mind for your own projects: build the measuring tape before
you start cutting.

## Kinds of invalidation

```rust
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Invalidation {
    #[default]
    None,
    Paint,
    Layout,
    Rebuild,
}
```

The variants are ordered from cheapest to most expensive, and that order is
used. Deriving **`PartialOrd`/`Ord`** on an enum without data compares variants
by **declaration order**: `None < Paint < Layout < Rebuild`. So "the more
expensive of two invalidations" could simply be `a.max(b)`. The code writes the
rule out explicitly anyway:

```rust
impl Invalidation {
    pub const fn merge(self, other: Self) -> Self {
        use Invalidation::*;

        match (self, other) {
            (Rebuild, _) | (_, Rebuild) => Rebuild,
            (Layout, _) | (_, Layout) => Layout,
            (Paint, _) | (_, Paint) => Paint,
            _ => None,
        }
    }
}
```

Why not `max`? Because `merge` is a `const fn`, and `Ord::max` is a trait method,
which can't be called in a `const fn` on stable Rust. The explicit `match` also
makes the policy readable, and doesn't silently depend on declaration order.
`use Invalidation::*;` inside a function imports the variants locally so the
arms stay short. Note `#[default]` on a variant: `#[derive(Default)]` for enums
picks the marked variant.

Some changes only need a repaint: a pressed button turning grey. Some need
layout: text that got longer. Some need the entity to render again. The cheapest
sufficient kind wins.

## Damage: a bounded list of rectangles

Damage is a set of screen rectangles that must be repainted:

```rust
const DAMAGE_RECT_CAPACITY: usize = 4;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DamageRegion {
    rects: RectList<DAMAGE_RECT_CAPACITY>,
    full: bool,
}
```

Notice the **capacity of four**. An arbitrary set of rectangles could grow
without limit, so InkPaper keeps at most four, merging as it goes:

```rust
pub fn add_rect(mut self, rect: Rect) -> Self {
    if self.full || rect.width().is_non_positive() || rect.height().is_non_positive() {
        return self;
    }

    let mut candidate = rect;
    let mut index = 0usize;

    while index < self.rects.len() {
        let existing = self.rects.as_slice()[index];

        if damage_rects_touch_or_overlap(candidate, existing) {
            candidate = damage_rect_union(candidate, existing);

            self.rects.remove(index);

            /*
             * The enlarged candidate can now overlap
             * a rectangle we checked earlier, so
             * restart the small bounded scan.
             */
            index = 0;
        } else {
            index += 1;
        }
    }

    let Err(candidate) = self.rects.push(candidate) else {
        return self;
    };

    // damage storage is deliberately bounded. If it fills up, correctness wins over
    // precision: collapse all rectangles into one conservative bounding rectangle.
    let collapsed = self
        .rects
        .as_slice()
        .iter()
        .copied()
        .fold(candidate, damage_rect_union);

    self.rects.clear();
    self.rects
        .push(collapsed)
        .expect("an emptied damage list has room");

    self
}
```

There are a few lessons here.

**Correctness over precision.** When the list is full, the code *over-approximates*
with one big bounding box. Repainting too much is a performance issue, while
repainting too little leaves a stale image on screen, which is a bug. Whenever
you bound a data structure, decide which way it errs.

**`let Err(candidate) = ... else { return self; };`** is a neat reversal of the
usual pattern. `push` returns `Err(rect)` and gives the rectangle *back* when the
list is full. Success is the boring case and exits early, and the code below
handles the full list. Returning ownership of a value inside the error is an
idiom worth copying: the caller never loses the thing it tried to insert.

**`fold`** reduces an iterator to one value: start with `candidate`, and union in
each existing rectangle. `.copied()` turns an iterator of `&Rect` into one of
`Rect`, since `Rect` is `Copy`.

**Methods that take and return `self`.** `DamageRegion` is `Copy`, and its
methods consume and return it (`damage = damage.add_rect(r)`), just like the
style builders of chapter 2. For small values this is often clearer than
`&mut self`, and it allows `const fn`.

### A list that can't leak its empty slots

`RectList<N>` is a fixed array plus a length:

```rust
/// up to `N` rectangles, kept in insertion order and copied by value. Only the stored
/// rectangles are reachable, so free slots can't leak into iteration or comparison
#[derive(Clone, Copy)]
pub(super) struct RectList<const N: usize> {
    rects: [Rect; N],
    len: u8,
}

impl<const N: usize> RectList<N> {
    const LEN_FITS_U8: () = assert!(N <= u8::MAX as usize, "capacity must fit in a u8");

    pub(super) const fn new() -> Self {
        #[allow(clippy::let_unit_value)]
        let () = Self::LEN_FITS_U8;

        Self {
            rects: [EMPTY_RECT; N],
            len: 0,
        }
    }

    pub(super) fn as_slice(&self) -> &[Rect] {
        &self.rects[..self.len()]
    }
}
```

Two techniques here.

**A compile-time assertion that depends on a const generic.** `const LEN_FITS_U8:
() = assert!(...)` is an associated constant whose evaluation runs the assert.
Associated constants are evaluated lazily, only when used, and `new()` uses it
with `let () = Self::LEN_FITS_U8;`. So `RectList::<300>::new()` fails *at compile
time*, while `RectList::<4>` compiles fine. This is the standard trick for
checking const-generic parameters on stable Rust.

**Only expose the valid prefix.** The struct can't derive `PartialEq`, because
that would compare the unused slots too, and two lists with the same rectangles
but different leftovers would be "unequal". So the module implements comparison
and iteration through `as_slice()` only. Commit `1d3ab08`, "keep damage rects in a
list that only exposes its stored rects", exists precisely to make that mistake
impossible.

## Painting only what's damaged

With damage known, painting can skip work. Over the course of 15–17 August the
commits went step by step:

- `3e77018` "skip painting outside damage": a node whose bounds don't touch the
  damage isn't painted.
- `edddf01` "cache subtree paint extents": each node remembers the rectangle
  covered by its whole subtree (children can overflow their parent), so a whole
  subtree can be skipped with one comparison.
- `2604849` "prune ordered sibling tails" and `9998232` "skip ordered sibling
  prefixes": in a column, children are sorted by position, so once one child is
  below the damage, all following ones are too.

Each one is a small, measurable step, and each one has a benchmark in
`crates/ui/benches/partial_paint_*`.

## Damage meets the display

Displays have rules of their own. The X4's controllers update windows whose
horizontal edges fall on byte boundaries (8 pixels per byte in a 1-bit plane).
`RefreshRegionConstraints` turns logical damage into damage the panel can
actually refresh:

```rust
pub fn normalize(self, damage: DamageRegion) -> DamageRegion {
    if damage.is_none() {
        return DamageRegion::none();
    }

    // full damage is semantic, not merely geometric.
    // do not convert it into a rectangle coverting `bounds` because that would turn
    // a requested full refresh into a partial refresh covering the whole screen
    if damage.is_full() {
        return DamageRegion::full();
    }
    // ... clip each rect to the screen, align it outward, add it back ...
}
```

The comment makes a sharp point: **"full" is a meaning, not a size.** A full
refresh uses a different, flashing waveform that clears ghosting. A partial
refresh that happens to cover the whole screen does not. Encoding "full" as a
flag instead of "a rectangle as big as the screen" keeps that difference in the
type.

## Choosing a refresh in the firmware

The firmware turns all of this into a decision, in
`firmware/x4-pro/src/firmware/refresh_policy.rs`:

```rust
pub fn select(&mut self, context: RefreshContext) -> RefreshRequest {
    let request = if context.total_pixels == 0 {
        // a malformed/unknown display size should fail conservatively.
        RefreshRequest::Full
    } else if context.grayscale_requires_full() {
        RefreshRequest::Full
    } else if self.consecutive_fast_refreshes >= MAX_CONSECUTIVE_FAST_REFRESHES {
        // periodically re-establish the panel using the complete waveform, regardless
        // of whether the current content is binary or grayscale.
        RefreshRequest::Full
    } else if context.binary_differential_eligible() || context.grayscale_window_eligible() {
        RefreshRequest::Fast
    } else if context.full_damage
        || ratio_at_least(context.damaged_pixels, context.total_pixels, LARGE_DAMAGE_PERCENT)
        || (context.continuous_tone_images
            && ratio_at_least(
                context.damaged_pixels,
                context.total_pixels,
                CONTINUOUS_TONE_DAMAGE_PERCENT,
            ))
    {
        RefreshRequest::Full
    } else {
        RefreshRequest::Fast
    };

    self.record(request);

    request
}
```

`if` is an **expression** in Rust, so a long `if / else if` chain can produce a
value directly (`let request = if ... { A } else { B };`). Each branch carries a
comment saying *why*, which turns a policy that would otherwise look arbitrary
into documentation of the hardware's behaviour. After twelve fast refreshes, a
full one clears accumulated ghosting.

The helpers use `matches!`, a macro that returns `true` if a value matches a
pattern, here a struct-like enum variant with a literal field:

```rust
const fn binary_differential_eligible(self) -> bool {
    matches!(
        self.content,
        RefreshContent::Binary {
            differential_eligible: true,
        }
    )
}
```

And the percentage check avoids division, and with it rounding and
divide-by-zero, by cross-multiplying in `u64`, the same technique as the flex
ratios of chapter 7:

```rust
fn ratio_at_least(part: u32, total: u32, percent: u8) -> bool {
    if total == 0 {
        return false;
    }

    u64::from(part).saturating_mul(100) >= u64::from(total).saturating_mul(u64::from(percent))
}
```

## Partial rebuilds: rendering only what changed

Damage limits *painting*. But until October, any `notify()` still rebuilt the
*entire* element tree: every entity rendered again, every node was mounted again.
On a microcontroller that wastes time and battery when only the clock ticked.

The October 4 commits changed that in four steps:

1. **Dirty flags per entity** (`eb84e0d`). `notify()` marks one entity, not the
   whole runtime.
2. **Rebuild only dirty entities** (`452e5b7`). The expansion loop from chapter 6
   reuses the previous nodes of entities that don't need to render.
3. **Damage only what changed** (`4018eae`).
4. **Track reads** (`136a64f`). If entity A's render *read* entity B, then
   changing B must re-render A, even though A itself never notified.

The fourth step is the most interesting. Remember `record_read` in
`Entity::try_read` from chapter 3:

```rust
cx.runtime.record_read(Source::Entity(self.id));
```

Every read during a render is recorded as an edge "reader → source":

```rust
/// state a render can read
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Entity(EntityId),
    Global(TypeId),
}

impl<V: VecStorage<Dependency>> DependencyStore for Dependencies<V> {
    fn record(&self, reader: EntityId, source: Source) {
        if source == Source::Entity(reader) {
            return;
        }

        let dependency = Dependency { reader, source };
        let mut edges = self.edges.borrow_mut();
        if edges.contains(&dependency) {
            return;
        }

        if edges.try_push(dependency).is_err() {
            self.incomplete.set(true);
        }
    }
    // ...
}
```

and `notify` walks the edges backwards:

```rust
/// marks `entity` and the entities that read it as needing to render again
pub(crate) fn notify(self, entity: EntityId) {
    self.entities.mark_dirty(entity);
    self.mark_readers(Source::Entity(entity));
}
```

This is the same idea as signals in modern web frameworks or the dependency
tracking in a spreadsheet, built from a flat vector of pairs. When the vector is
full, an `incomplete` flag makes the next change re-render *everything*. That's
the same "err on the side of correctness" rule as the damage list.

Then the app was reshaped to profit from it. Commit `01cfa86`, "split the battery
and clock into entities and enable partial rebuilds", moved the battery display
into its own `BatteryIndicator` entity (we saw it in chapter 3). Now a battery
reading re-renders and repaints a 60-pixel label, not the whole screen. The app's
comment explains a related choice in `InkPaperApp::render`:

```rust
// read only while shown, so the app renders again for them only then
let (battery, clock) = if control_center_open {
    let battery = self.battery.read(cx, |battery| battery.get());

    (battery, self.local_clock(cx))
} else {
    (None, None)
};
```

Since reads are *recorded*, not reading something when it isn't displayed is an
optimisation. The minute ticks of the clock only wake the display on screens
that show the clock.

## Rust concepts in this chapter

- **Derived ordering on enums** (declaration order), `#[default]` variants,
  `use Enum::*` in a scope.
- **`const fn` limits** (no trait method calls like `Ord::max` on stable).
- **Bounded collections** that degrade conservatively, and returning ownership in
  errors (`Err(rect)`).
- **`let Err(x) = ... else`**, **`fold`**, **`copied`**.
- **Compile-time assertions on const generics** via associated constants.
- **Why not to derive** `PartialEq` on a struct with unused storage.
- **`if` as an expression**, **`matches!`**.
- **Dependency tracking** with interior mutability (`RefCell`, `Cell`) behind
  `&self`.

## Try it yourself

1. In `crates/ui/src/rendering/invalidation/tests.rs`, add five disjoint
   rectangles to a `DamageRegion` and check that you get one bounding rectangle.
   Then add four and check that you get four.
2. Write `RectList::<300>::new()` in a test and read the compile-time error.
3. The simulator seeds a fake battery reading in `seed_system_status`
   (`crates/sim/src/main.rs`). Call `apply_battery_status` again a few seconds
   later from the main loop with a different percentage, and watch only the
   header label repaint. Then find in `crates/app/src/system.rs` why a reading
   that keeps the same whole percentage causes no repaint at all.
