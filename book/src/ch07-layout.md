# 7. Layout with integers

> **Where we are:** commit `c8d2faf`, "add deterministic box and flex layout",
> 9 August 2026, then its growth over the following week (`cd775ca` margins and
> constraints, `392be6a` weighted grow and shrink, `3799255` overflow clipping,
> `075f5b6` "encapsulate pixel arithmetic"), and two refactors from 30 October:
> `8638df7` ("make unbounded layout space a variant instead of a huge number")
> and `f1094be` ("give each layout box its own type").

## What layout has to do

After chapter 6 we have a frame: a tree of nodes with styles, but no positions.
**Layout** gives every node a rectangle. InkPaper implements a small subset of
CSS: block flow, flexbox (row and column, grow and shrink, alignment, gaps),
padding, borders, margins, min/max sizes, absolute positioning and scroll
containers.

The commit title says *deterministic* layout, and that word drives the design.
The same tree must produce the same pixels on a laptop and on the ESP32-S3,
in the simulator and in tests, every time. That's why the layout engine uses
**integers, not floats**.

## Pixel arithmetic as a type

Chapter 2's `Pixels(pub i32)` had a public field, so anyone could write
`Pixels(a.0 + b.0)`. On 11 August, commit `075f5b6`, "encapsulate pixel
arithmetic", made the field private and gave `Pixels` its own operators:

```rust
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Pixels(i32);

impl Pixels {
    pub const ZERO: Self = px(0);
    pub const MAX: Self = px(i32::MAX);

    pub const fn get(self) -> i32 {
        self.0
    }

    pub const fn saturating_add(self, other: Self) -> Self {
        Self(self.0.saturating_add(other.0))
    }

    pub const fn non_negative(self) -> Self {
        Self(if self.0 < 0 { 0 } else { self.0 })
    }
    // ...
}

impl Add for Pixels {
    type Output = Self;

    fn add(self, rhs: Self) -> Self::Output {
        self.saturating_add(rhs)
    }
}

impl Sub for Pixels {
    type Output = Self;

    fn sub(self, rhs: Self) -> Self::Output {
        self.saturating_sub(rhs)
    }
}

impl AddAssign for Pixels {
    fn add_assign(&mut self, rhs: Self) {
        *self = self.saturating_add(rhs);
    }
}

impl Mul<i32> for Pixels {
    type Output = Self;

    fn mul(self, rhs: i32) -> Self::Output {
        self.saturating_mul(rhs)
    }
}
```

This is **operator overloading** through the traits in `core::ops`. Writing
`impl Add for Pixels` makes `a + b` call `Pixels::add(a, b)`. The `type Output`
associated type says what the expression produces. `Mul<i32>` takes a type
parameter for the right-hand side, so `width * 2` compiles but `width * width`
doesn't, and pixels times pixels would be an area anyway.

The key decision is that **every operator saturates**. A plain `i32` addition
that overflows panics in debug builds and wraps in release builds. With
deep nesting, huge scroll offsets or a bogus style, a layout engine *will*
eventually add two big numbers. Saturating means the result sticks at
`i32::MAX`, which renders as "very far off-screen" instead of a crash or a
negative width. On a device where you can't attach a debugger to every user's
reader, "degrade, don't crash" is the right default.

`non_negative()` shows up all over the layout code. Sizes can't be negative, but
`available - used` can go below zero. Clamping at the point of use makes each
formula read like the CSS spec it implements.

The derives include `PartialOrd, Ord`, so `Pixels` can be compared and used with
`max`/`min`. `Ord` requires a *total* order, which `f32` can't offer (`NaN` isn't
comparable). That's one more practical reason to prefer integers here.

### Ratios without floats

Images must be scaled while keeping their aspect ratio. Without floats, that's
integer multiplication followed by division, done in a wider type so the product
can't overflow:

```rust
pub(crate) fn scale_ratio_floor(&self, numerator: Pixels, denominator: Pixels) -> Self {
    let value = i64::from(self.0.max(0));
    let numerator = i64::from(numerator.0.max(0));
    let denominator = i64::from(denominator.0.max(0));

    if value == 0 || numerator == 0 || denominator == 0 {
        return px(0);
    }

    let result = value.saturating_mul(numerator) / denominator;

    Self(i32::try_from(result).unwrap_or(i32::MAX))
}
```

`i64::from(x)` is a **lossless conversion**. The `From` impl exists only when
every `i32` fits in an `i64`, so it can never fail. Going back down uses
`i32::try_from`, which *can* fail, and the code chooses what to do on failure:
saturate. This `from`-up, `try_from`-down discipline is exactly what the cast
lints from chapter 1 push for.

## Saying "no limit" honestly

Here is a story from late in the project that shows how Rust's types can carry
meaning.

Layout passes *available space* down the tree: "you may be at most 300 pixels
wide". But sometimes there is no limit, for instance along the scrolling axis of
a scroll container, where content can be as tall as it likes. For almost three
months InkPaper represented "no limit" as a very large number, `Pixels::MAX`.
That's a **sentinel value**, and sentinels cause trouble. Code that subtracts
padding from `MAX` gets a number that is no longer `MAX`, so it no longer means
"unlimited". Code that adds to it saturates. Every function needs to remember the
convention.

Commit `8638df7` replaced the sentinel with an enum:

```rust
/// the space a node may take along one axis
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AvailableSpace {
    /// at most this much
    Definite(Pixels),
    /// as much as the content takes, such as along a scroll container's scrolling axis
    Unbounded,
}

impl AvailableSpace {
    pub const fn definite(self) -> Option<Pixels> {
        match self {
            Self::Definite(space) => Some(space),
            Self::Unbounded => None,
        }
    }

    /// `size`, cut down to fit the space
    pub const fn limit(self, size: Pixels) -> Pixels {
        match self {
            Self::Definite(space) => size.min(space.non_negative()),
            Self::Unbounded => size,
        }
    }

    /// the space left after `used`
    const fn shrink(self, used: Pixels) -> Self {
        match self {
            Self::Definite(space) => Self::Definite(space.saturating_sub(used).non_negative()),
            Self::Unbounded => Self::Unbounded,
        }
    }
}
```

Now subtracting padding from unbounded space is `Unbounded.shrink(padding)`,
which stays `Unbounded` by construction. Any code that needs a number has to call
`definite()` and handle `None`, so the compiler makes every caller think about
the unlimited case. The commit touched fourteen files. Each change was a place
where the old sentinel could have been mishandled.

The text measurer's signature changed with it. Before:

```rust
fn measure_text(&self, text: &str, style: ResolvedTextStyle, max_size: Size) -> Size;
```

After:

```rust
fn measure_text(
    &self,
    text: &str,
    style: ResolvedTextStyle,
    available_width: AvailableSpace,
) -> Size;
```

The old version needed a paragraph of documentation explaining what `max_size`'s
height meant. The new one says it in the types: text wraps against a width that
may or may not be limited, and its height is whatever its lines take.

**Lesson:** when you find yourself writing "if the value is X, it means
something special", reach for an enum.

## Boxes with names

CSS has nested boxes: the *border box* (the node's full rectangle), the *padding
box* inside the border, and the *content box* inside the padding. Children are
placed in the content box, but clipped and scrolled against the padding box.
Mixing them up produces bugs that are off by a few pixels, the hardest kind to
notice.

Commit `f1094be` gave each box its own newtype:

```rust
/// a node's layout bounds, border included
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct BorderBox(Rect);

/// the area inside a node's border. Its children clip to it and scroll within it
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct PaddingBox(Rect);

/// the area inside a node's padding, where its children are placed
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ContentBox(Rect);

impl BorderBox {
    /// `style` is `None` for nodes that have no border, such as text
    pub(crate) fn padding_box(self, style: Option<Style>) -> PaddingBox {
        PaddingBox(
            self.0
                .inset(style.map(border_width).unwrap_or(Pixels::ZERO)),
        )
    }

    pub(crate) fn content_box(self, style: Style) -> ContentBox {
        // ...
    }
}
```

They are all `Rect`s underneath, but you can only get a `PaddingBox` from a
`BorderBox` by calling `padding_box`, which subtracts the border. A function that
needs the padding box declares a `PaddingBox` parameter, and passing the border
box is a compile error. The module's doc comment says it plainly: "Each is its
own type, so code that measures against one of them has to say which."

## Flexbox in integers

The heart of the layout engine is flex distribution. Each child has a *base size*.
If the children together are smaller than the container, the free space is
**grown** into them in proportion to their `flex_grow` weights. If they are
larger, the overflow is **shrunk** out of them. Each child also has a min and a
max, so a child that hits its limit gets *frozen* there, and its share is
redistributed among the others.

The code keeps exact integer arithmetic throughout. The current state of
distribution is a ratio, `amount / weight`, kept as a *fraction* instead of being
divided early:

```rust
/// space per unit of weight, `amount / weight`
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct FlexRatio {
    amount: Pixels,
    weight: u64,
}

impl FlexRatio {
    /// whether this ratio gives `weight` more than `limit`. Once no weight is left
    /// to share, every flexible child is at its limit
    fn passes(self, limit: Pixels, weight: u64) -> bool {
        if self.weight == 0 {
            return true;
        }

        u128::from(unsigned(limit)) * u128::from(self.weight)
            < u128::from(unsigned(self.amount)) * u128::from(weight)
    }
}
```

To compare `limit / weight` with `amount / self.weight` without dividing, it
cross-multiplies, in `u128` so the product can never overflow. `u128` is a native
Rust type, even on a 32-bit microcontroller where the compiler emulates it with a
few instructions. That costs almost nothing here and removes a whole category of
overflow reasoning.

The freezing loop is short and worth reading:

```rust
// each pass freezes the children the last ratio took past their limits at
// those limits, and shares what's left between the rest. Freezing children
// only raises the ratio, so the frozen set only grows until the ratio settles
loop {
    let mut frozen = px(0);
    let mut weight = 0u64;
    let mut current = self.node(parent).first_child;

    while let Some(child) = current {
        if self.node_position(child) != Position::Absolute {
            let base = self.flex_base_main_size(child, axis, available, text_measurer);
            let share = self.flex_share(child, axis, base, mode);

            match share.frozen_at(line.ratio) {
                Some(limit) => frozen += limit,
                None => weight = weight.saturating_add(share.weight),
            }
        }

        current = self.node(child).next_sibling;
    }

    let ratio = FlexRatio {
        amount: (amount - frozen).non_negative(),
        weight,
    };
    if ratio == line.ratio {
        return Some(line);
    }

    line.ratio = ratio;
}
```

`loop` with no condition runs until a `return` or `break`. The comment carries the
*termination argument*: the frozen set only grows, and there are finitely many
children, so the loop must end. Writing down why a loop terminates is a good
habit whenever the condition isn't obvious.

### Never lose a pixel

Dividing 100 pixels among three equal children gives 33.33... each. Rounding each
share down gives 99, and one pixel goes missing. The code avoids this by
computing *cumulative* shares and taking differences:

```rust
fn weighted_share(
    amount: Pixels,
    previous_weight: u64,
    next_weight: u64,
    total_weight: u64,
) -> Pixels {
    if amount.is_non_positive() || total_weight <= previous_weight {
        return px(0);
    }

    let amount = u128::from(unsigned(amount));
    let total_weight = u128::from(total_weight);
    let previous = amount * u128::from(previous_weight) / total_weight;
    let next = amount * u128::from(next_weight) / total_weight;
    let share = next.saturating_sub(previous);

    px(i32::try_from(share).unwrap_or(i32::MAX))
}
```

Child *i* gets `floor(amount × weights up to and including i / total) −
floor(amount × weights before i / total)`. The differences telescope: summed over
all children they give exactly `amount`. For 100 pixels and three children the
shares are 33, 33 and 34. The caller threads the running weight through a `&mut
u64` parameter (`before: &mut u64` in `flex_item_main_size`). That's a small
example of an **out-parameter** in Rust: a mutable reference used to carry
state across calls.

### Shrinking fairly

```rust
// a child gives up overflow in proportion to its size, so small children
// aren't squeezed to nothing first
FlexMode::Shrink => FlexShare {
    weight: (self.node_flex_shrink(child, axis) as u64)
        .saturating_mul(u64::from(unsigned(base))),
    limit: Some((base - minimum.unwrap_or(Pixels::ZERO)).non_negative()),
},
```

That's the CSS rule: the shrink weight is `flex_shrink × base size`. Note
`minimum.unwrap_or(Pixels::ZERO)`, a default for a missing `Option`, and the
`as u64` cast from a `u16`, which can never lose information. The cast lints only
flag casts that *can*.

## Measuring once

Flex layout asks "how big would this child like to be?" many times, and for
text that means shaping and line-breaking, which is expensive. Commit `ce39cc3`,
"cache node measurements within layout passes", added a cache. Commit `b39ef27`,
much later, refined it to "measure text once per width during layout":

```rust
if let Some(measured) = self.cached_measurement(node, available) {
    count_metric!(self, measurement_cache_hits);

    return measured;
}

count_metric!(self, measurement_cache_misses);
```

`count_metric!` is a macro that compiles to nothing unless the `metrics` feature
is on. The project has benchmarks (`crates/ui/benches/`) that count layout work
precisely, so optimisations like this one are measured, not guessed. We return to
measurement in chapter 20.

## Small idioms worth stealing

A few lines from the layout code that use the standard library well:

```rust
let Some(style) = self.node_flex_style(node) else {
    return 0;
};
```

`let ... else` again: bail out early when there is no style.

```rust
if !weights
    .tightest
    .is_some_and(|tightest| line.ratio.passes(tightest.amount, tightest.weight))
{
    return Some(line);
}
```

`Option::is_some_and` tests the value inside an `Option` without unwrapping it.

```rust
let (right_padding, bottom_padding) = style.map_or((Pixels::ZERO, Pixels::ZERO), |style| {
    (style.padding.right.non_negative(), style.padding.bottom.non_negative())
});
```

`Option::map_or(default, f)` returns `default` for `None`, otherwise `f(value)`,
and here it returns a tuple that is destructured right away.

## Rust concepts in this chapter

- **Operator overloading** with `Add`, `Sub`, `AddAssign`, `Mul<Rhs>` and
  `type Output`.
- **Saturating, checked and wrapping arithmetic**, and choosing among them.
- **`PartialOrd` vs `Ord`**, and why integers suit deterministic layout.
- **Lossless `From` and fallible `TryFrom` conversions**; widening to `i64`/`u128`.
- **Enums instead of sentinel values** (`AvailableSpace`).
- **Newtypes for meaning** (`BorderBox`, `PaddingBox`, `ContentBox`).
- **`loop` with a documented termination argument.**
- **Out-parameters** with `&mut`.
- **`Option` combinators**: `map`, `unwrap_or`, `map_or`, `is_some_and`.

## Try it yourself

1. Find `fn weighted_share` in `crates/ui/src/layout/flex.rs` and write a unit
   test checking that the shares of 100 pixels over weights `[1, 1, 1]` sum to
   100. Then try `[1, 2, 3, 4]` with 7 pixels.
2. Check out the commit before `8638df7` (`git checkout 8638df7~1`) and search
   for `Pixels::MAX` in `crates/ui/src/layout`. For each use, ask: what would
   happen if someone subtracted padding from it first?
3. Make a `Celsius(i32)` newtype that implements `Add` and `Sub` but not `Mul`,
   and explain in a comment why multiplying temperatures makes no sense.
