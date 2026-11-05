# 7. Making every pixel fit

A settings row has a label, a selected value, and a little room between them. Give the label too much width and the value disappears; give it too little and a short phrase wraps unnecessarily. Below it, a scrolling list may contain more content than the display can show. The screen is finite, but the list must still be measured beyond its visible edge.

Layout turns those competing intentions into rectangles. InkPaper supports box flow, row and column flex layout, constraints, gaps, margins, absolute positioning, and scrolling. The interesting part is not how many familiar style names it offers. It is how the engine makes choices precise enough that a one-pixel remainder, an absent limit, and a border all have an unambiguous meaning.

We will follow three questions: what arithmetic is safe, what space is actually available, and who receives the leftover pixels. These excerpts come from the current layout implementation and are not standalone programs.

## A pixel is a small policy

InkPaper represents distances with `Pixels`, a wrapper around `i32`, rather than passing bare integers everywhere. Its private field discourages callers from bypassing the chosen arithmetic. The `px` constructor creates a value; `get` retrieves the integer when a lower-level interface needs it.

This exact implementation from `crates/ui/src/geometry.rs` supplies addition:

```rust,ignore
impl Add for Pixels {
    type Output = Self;

    fn add(self, rhs: Self) -> Self::Output {
        self.saturating_add(rhs)
    }
}
```

`Add` is the trait behind Rust's `+` operator. Its associated `Output` type says that adding pixels produces pixels. Subtraction and multiplication by an integer likewise use saturating arithmetic. A very large sum stops at the representable boundary rather than wrapping around to a surprising negative coordinate. The engine separately uses `non_negative()` where a result represents a size or remaining room: a coordinate may legitimately be negative, while a usable width cannot.

Saturation is containment, not a proof that the layout is sensible. If a calculation reaches an extreme value, the result may still be off-screen or badly proportioned. Checked arithmetic returning an error would make numerical failure more explicit, but would also put error handling throughout ordinary layout formulas. Here the chosen policy keeps arithmetic bounded and leaves clipping and constraints to contain the visual result.

Integers also make the final rounding decisions visible. They are not the only way to obtain deterministic layout; a floating-point engine can define a careful rounding policy too. InkPaper's choice means whole-pixel positions and allocation rules are part of the calculation itself, rather than a conversion postponed until painting. Other parts of rendering can still need more detailed geometry; this is a choice about the UI layout model.

## No room is not unlimited room

A list's viewport might be 400 pixels tall while its contents are 900 pixels tall. Measuring those contents against a hard 400-pixel limit would confuse the visible window with the amount of content that exists. Conversely, passing zero must not mean “take whatever you want.” Zero is a perfectly definite lack of room.

The exact enum in `crates/ui/src/layout/mod.rs` makes the distinction explicit:

```rust,ignore
/// the space a node may take along one axis
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum AvailableSpace {
    /// at most this much
    Definite(Pixels),
    /// as much as the content takes, such as along a scroll container's scrolling axis
    Unbounded,
}
```

`Definite(px(0))` means there is a bound, and it is zero. `Unbounded` means there is no limit supplied on that axis. Neither is a request to create infinitely large content. The content still has to determine its size. The distinction is useful for text too: `TextMeasurer::measure_text` receives an `AvailableSpace` for width, so wrapping constraints do not need to be hidden inside a misleading height value.

The following exact methods show what happens when padding consumes some room:

```rust,ignore
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
```

A `match` must account for both meanings. Shrinking a finite allowance can reach zero; shrinking an unbounded allowance remains unbounded. A very large sentinel integer would not preserve that property automatically: subtracting padding from the sentinel makes an ordinary large number. An enum lets the operation preserve the meaning instead of relying on every caller to remember a convention.

There is still a real policy decision for consumers. `definite()` returns `Option<Pixels>`, and a caller receiving `None` must decide how its particular content measures without a limit. Types expose the question; they cannot answer it for every kind of content.

## Which edge are we measuring from?

Return to the settings row. Its border occupies space, its padding separates text from that border, and its children begin farther inward. Using the same rectangle for all three purposes would be convenient until clipping sliced through the wrong edge.

InkPaper gives the nested boxes distinct types. These are exact declarations from `crates/ui/src/layout/boxes.rs`:

```rust,ignore
/// a node's layout bounds, border included
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct BorderBox(Rect);

/// the area inside a node's border. Its children clip to it and scroll within it
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct PaddingBox(Rect);

/// the area inside a node's padding, where its children are placed
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ContentBox(Rect);
```

This is the newtype pattern: identical underlying representation, different meaning at function boundaries. `BorderBox::padding_box` subtracts border width. `content_box` additionally accounts for padding. Child placement uses the content box; the viewport for clipping and scrolling is the padding box.

These wrappers do not magically validate every rectangle. Constructors and conversions still need correct arithmetic, and extracting a plain `Rect` deliberately loses the distinction. Their benefit is local: code accepting a `PaddingBox` states which boundary it expects. A reviewer need not reconstruct that intention from a variable named merely `bounds`.

Absolute positioning adds another useful distinction. The containing block stores both padding and content information: explicit insets measure from the positioned ancestor's padding box, while an axis without insets starts at its content origin. “Position this child inside that parent” is not enough detail until the relevant edge is named.

## Sharing the last pixel

Flexible children begin with base sizes. If space remains, grow weights divide it; if the children overflow, shrink weights decide what they give up. Minimum and maximum sizes may prevent a child from taking its initial share. The engine freezes such children at their limits and redistributes the remainder among the others. Absolute children are not participants in this flow distribution.

Even without limits, division needs care. One hundred extra pixels shared equally by three children cannot give each child exactly one third. Independently rounding down gives 99 pixels. Instead, `weighted_share` computes rounded cumulative positions and subtracts them. Here is its exact implementation from `crates/ui/src/layout/flex.rs`:

```rust,ignore
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

For three equal weights the cumulative boundaries are 0, 33, 66, and 100, producing widths of 33, 33, and 34. The shared boundaries ensure that the pieces fit together. They also make the bias explicit: the last child gets the remainder in this example. An alternative could distribute remainder pixels by a different priority, but it would still need a rule.

The intermediate `u128` arithmetic keeps products wide; `From` performs lossless widening, while `try_from` acknowledges that narrowing can fail. Wide integer arithmetic has a target-dependent cost, so this is not a claim that larger types are free. It is a way to keep the fraction calculation accurate before returning to pixel-sized values.

Shrink weights also account for base size, not only the configured shrink factor. Equally weighted children of very different sizes therefore do not necessarily lose the same number of pixels. Layout is balancing room against constraints, not simply dividing the display into equal slots.

## Try it: account for seven pixels

For an optional read-only exercise, follow `weighted_share` with `amount = 7` and weights `[1, 2, 3, 4]`. The cumulative weights are 0, 1, 3, 6, and 10. Integer division gives boundaries 0, 0, 2, 4, and 7; the shares are therefore 0, 2, 2, and 3. Their sum remains seven even though the first child receives none of this extra space.

Then follow `AvailableSpace::shrink` with an allowance of five pixels and eight pixels of chrome. The finite result is zero; the unbounded result stays unbounded. These two small traces expose the engine's promises without fonts, a simulator, or hardware: do not lose a distributed pixel, and do not confuse absence of a limit with absence of room.
