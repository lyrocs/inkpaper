# 16. Fonts and glyphs

> **Where we are:** `bc8e06d` "add font resource foundation" (27 August 2026),
> `f4122ac` "add ttf rasterization" (28 August), `b11ed75` "add coverage
> compositing and dithering", the glyph cache work of 18 September (`e9de933`,
> `715f92a` "use linear probing for glyph cache"), variable fonts (`23a4a39`),
> autohinting (`66173d8`, `a4067a5`, 28 September), and the text darkness and
> antialiasing settings (17 October).

## From a letter to pixels

Until late August, text was drawn with `embedded-graphics`' built-in bitmap
fonts: fixed sizes, ASCII only, a little crude. A reader needs real typography:
TrueType/OpenType fonts at any size, any weight, with kerning. That means:

1. **Parse** the font file to find each character's **glyph**: an outline made
   of lines and Bézier curves.
2. **Rasterize** the outline at a pixel size into a **coverage** bitmap: for each
   pixel, how much of it the shape covers (0–255).
3. **Composite** the coverage onto the screen. On a 4-grey e-ink panel, that means
   deciding which pixels get ink.
4. **Cache** the result, because rasterizing is expensive and text repeats.

Parsing uses the `ttf-parser` crate, which is `no_std`, zero-copy (it reads
directly from the font's bytes) and allocation-free. The rest is InkPaper's own
code.

## A trait for faces

All font kinds, the old bitmap fonts and real TrueType, sit behind a trait:

```rust
pub trait FontFace {
    fn weight_range(&self) -> FontWeightRange {
        FontWeightRange::default()
    }

    fn style(&self) -> FontStyle {
        FontStyle::Normal
    }

    /// resolve one unicode scalar value to a font-local glyph.
    fn glyph_id(&self, character: char) -> Option<GlyphId>;

    fn metrics(&self, size_px: u16) -> FontMetrics;

    /// return only the horizontal advance
    ///
    /// the default is convenient for bitmap fonts. Scalable fonts should override this
    // ...
}
```

The default methods follow chapter 2's pattern: a bitmap font only implements the
essentials, and a variable TrueType font overrides `weight_range` to announce
that it covers, say, weights 100 to 900. `glyph_id` returns an `Option`: a font
can simply lack a character (a Latin font has no Arabic letters), and the caller
must decide what to do, such as falling back to another face.

## Rasterizing with a fixed budget

Here is how the rasterizer is told about an outline. `ttf-parser` defines a
trait, and the rasterizer implements it:

```rust
impl OutlineBuilder for ScanlineBuilder {
    fn move_to(&mut self, x: f32, y: f32) {
        let point = self.transform(x, y);

        self.current = Some(point);
        self.contour_start = Some(point);
    }

    fn line_to(&mut self, x: f32, y: f32) {
        let to = self.transform(x, y);

        if let Some(from) = self.current {
            self.add_segment(from, to);
        }

        self.current = Some(to);
    }

    fn quad_to(&mut self, x1: f32, y1: f32, x: f32, y: f32) {
        let end = self.transform(x, y);

        let Some(start) = self.current else {
            self.current = Some(end);

            return;
        };

        let control = self.transform(x1, y1);

        flatten_quad(start, control, end, |from, to| self.add_segment(from, to));

        self.current = Some(end);
    }
    // curve_to and close are similar
}
```

This is the **visitor pattern** from chapter 15 again, defined by a *library*
this time. `ttf-parser` walks the glyph's outline and calls `move_to`,
`line_to`, `quad_to`, `curve_to` and `close`. It doesn't care what the visitor
does: compute a bounding box, write an SVG, or rasterize. Implementing a foreign
crate's trait for your own type is how you plug into a library's algorithms.

`flatten_quad(start, control, end, |from, to| self.add_segment(from, to))`
splits a curve into short straight segments and passes each to a **closure**
that borrows `self` mutably. The closure is a temporary `FnMut` argument, used
and dropped within the call, so the borrow is short and the compiler is happy.

The rasterizer is a **scanline** algorithm with 4×4 supersampling:

```rust
const SUPERSAMPLE_X: usize = 4;
pub(super) const SUPERSAMPLE_Y: usize = 4;
#[expect(clippy::cast_possible_truncation, reason = "16 samples")]
const COVERAGE_SAMPLES: u16 = (SUPERSAMPLE_X * SUPERSAMPLE_Y) as u16;

/// this is intentionally bounded
///
/// we don't allocate an edge/intersection vector while rasterizing. If a particular
/// outline exceeds the capacity, rasterization fails explecitly instead of silently
/// consuming heap memory.
///
/// Arabic/Latin glyphs are normally below this. If real fonts later demonstrate that
/// the value is too small, we can measure and tune it
const MAX_SCANLINE_INTERSECTIONS: usize = 96;
```

For each sample row, every outline segment that crosses it records where it
crosses and in which direction:

```rust
pub(super) fn add_segment(&mut self, from: RasterPoint, to: RasterPoint) {
    let winding = if from.y <= self.sample_y && self.sample_y < to.y {
        1
    } else if to.y <= self.sample_y && self.sample_y < from.y {
        -1
    } else {
        return;
    };

    let denominator = to.y - from.y;
    if denominator == 0.0 {
        return;
    }

    let t = (self.sample_y - from.y) / denominator;
    let x = from.x + (to.x - from.x) * t;

    self.push_intersection(Intersection { x, winding });
}
```

Crossings are sorted by `x`, and walking them left to right while summing the
windings tells you where you are *inside* the shape (non-zero winding) and where
you're outside. Count the inside samples per pixel, scale to 0–255, and you have
coverage.

The intersections live in a fixed array of 96, with an `overflowed` flag. The doc
comment states the policy: fail explicitly, don't allocate silently, and measure
before tuning. The sort is a hand-written insertion sort, with a comment
explaining why that's appropriate:

```rust
pub(super) fn sort_intersections(&mut self) {
    // intersection counts are intentionally tiny and bounded, so an allocation-free
    // insertion sort is appropriate here.
    let mut index = 1usize;

    while index < self.len {
        let value = self.intersections[index];
        let mut cursor = index;

        while cursor > 0 && self.intersections[cursor - 1].x > value.x {
            self.intersections[cursor] = self.intersections[cursor - 1];
            cursor -= 1;
        }

        self.intersections[cursor] = value;
        index += 1;
    }
}
```

(Why not `slice::sort_unstable_by`? It is in `core` and doesn't allocate either.
But `f32` isn't `Ord`, so it needs `partial_cmp` and a decision about NaN. For
fewer than a hundred items, insertion sort is simple and fast.)

### Floats in `no_std`

Chapter 7 praised integers, but rasterizing is inherently fractional: curves,
scales and sub-pixel positions. So the rasterizer uses `f32`, which the ESP32-S3
computes in hardware. But `no_std` Rust has a gap here. Methods like
`f32::floor` and `f32::sqrt` live in `std`, because on most platforms they call
the C math library. In `no_std` you need another implementation: the **`libm`**
crate, a pure-Rust port of the C math library. Commit `395d2de` ("replace
hand-rolled float math in ui with libm") swapped home-made approximations for it,
and a small module gathers the conversions:

```rust
//! float to integer rounding for pixel and font maths. `as` saturates at the
//! integer's bounds and turns NaN into 0, which is what these callers want, so the
//! casts the cast lints flag live here once

#![allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]

pub(crate) fn floor_i32(value: f32) -> i32 {
    libm::floorf(value) as i32
}

pub(crate) fn ceil_i32(value: f32) -> i32 {
    libm::ceilf(value) as i32
}
```

The module doc explains *why* `as` is acceptable here: float-to-int `as` casts
**saturate** and map NaN to 0 (that's defined behaviour in Rust, unlike C), which
is exactly what the callers want. The lint exceptions live in one small module
instead of being sprinkled across the crate. That's a good way to contain an
exception to a rule.

## The glyph cache

Rasterizing a glyph takes many microseconds. A page of text has thousands of
glyphs but only a few dozen distinct letters. So rasterized glyphs are cached:

```rust
pub struct GlyphCache<const SLOTS: usize, const BYTES: usize> {
    storage: [u8; BYTES],
    used: usize,
    slots: [GlyphCacheSlot; SLOTS],
    /// the registry revision the glyphs were rasterized from
    revision: u16,
    #[cfg(feature = "metrics")]
    metrics: GlyphCacheMetrics,
}
```

It's an **open-addressing hash table** with two const generic sizes: how many
glyphs (`SLOTS`) and how many bytes of bitmap (`BYTES`). The firmware's
`AppResources<'static, UI_GLYPH_CACHE_SLOTS, UI_GLYPH_CACHE_BYTES>` from chapter 13
picks them.

The `#[cfg(feature = "metrics")]` on a **struct field** removes the field
entirely in normal builds. Every use of it must be behind the same `cfg`, which
the `increment_metric!` macro handles:

```rust
increment_metric!(self.metrics.lookups);
```

The key combines everything that changes the bitmap: font, weight, glyph and
size. The hash is a hand-rolled mix:

```rust
fn slot_index(key: GlyphCacheKey) -> usize {
    let font = key.font.font().index();
    let weight = usize::from(key.font.weight().value());
    let glyph = usize::from(key.glyph.value());
    let size = usize::from(key.size_px);

    font.wrapping_mul(31)
        .wrapping_add(weight.wrapping_mul(13))
        .wrapping_add(glyph.wrapping_mul(17))
        .wrapping_add(size)
        % SLOTS
}
```

`wrapping_mul` and `wrapping_add` say explicitly "overflow is fine here, wrap
around". In a hash function that's the intent, and writing it out avoids
debug-build overflow panics.

The lookup uses **linear probing**. Commit `715f92a` replaced a direct-mapped
cache (one possible slot per key, so collisions evict each other) after the
metrics showed too many collisions:

```rust
let home_slot = Self::slot_index(key);
let mut insertion_slot = None;

// entries are never individually deleted, so encountering the first empty slot
// proves that the key is not present later in the probe sequence.
for probe in 0..SLOTS {
    let slot_index = (home_slot + probe) % SLOTS;
    let slot = self.slots[slot_index];

    if !slot.valid {
        insertion_slot = Some(slot_index);
        break;
    }

    if slot.key == key {
        increment_metric!(self.metrics.hits);

        return Ok(GlyphBitmap::new(
            &self.storage[slot.offset..slot.offset + slot.len],
            slot.metrics,
        ));
    }
}
```

The comment is the **correctness argument** for stopping at the first empty
slot. It holds *only because* entries are never deleted individually. The cache
is cleared all at once when full. Comments that state such invariants protect the
code from a future "optimisation" that adds single-entry deletion and silently
breaks lookups.

Look at the return type of `get_or_rasterize`:

```rust
pub fn get_or_rasterize<const FONTS: usize>(
    &mut self,
    registry: &FontRegistry<'_, FONTS>,
    font: impl Into<FontInstance>,
    glyph: GlyphId,
    size_px: u16,
) -> Result<GlyphBitmap<'_>, GlyphCacheError>
```

`GlyphBitmap<'_>` **borrows the cache's storage**. The caller gets a view of the
coverage bytes without copying them. Because the method takes `&mut self`, the
borrow checker guarantees that while you hold a bitmap, nobody can insert another
glyph and overwrite the bytes you're looking at. You must finish drawing one glyph
before fetching the next. That's exactly the right rule, and it costs nothing at
runtime.

### Invalidation by revision

Fonts can be unloaded (the user picks another font from the SD card), and font
ids may then be reused. The cache handles this with a **revision counter**:

```rust
// freed faces' ids may belong to other faces now
if self.revision != registry.revision() {
    self.clear();
    self.revision = registry.revision();
}
```

The registry bumps its revision whenever faces change, and the cache notices
lazily, at its next lookup. That's the generational idea from chapter 3 applied
to a whole cache.

## Ink on e-paper

Coverage is a value from 0 to 255, but the panel shows four levels, and the
fastest refresh mode only black and white. How should a pixel with coverage 140
be drawn?

The first answer (`b11ed75`) was **ordered dithering**, using a 4×4 Bayer matrix:

```rust
#[rustfmt::skip]
pub(crate) const BAYER_4X4: [u8; 16] = [
    0, 8, 2, 10,
    12, 4, 14, 6,
    3, 11, 1, 9,
    15, 7, 13, 5,
];

/// The 4x4 ordered-dither threshold at `(x, y)`, centered in its quantization
/// bucket (8, 24, 40, ... 248) and repeating every 4 pixels on both axes.
pub const fn bayer_4x4_threshold(x: i32, y: i32) -> u8 {
    let rank = BAYER_4X4[(y.rem_euclid(4) * 4 + x.rem_euclid(4)).unsigned_abs() as usize];

    rank * 16 + 8
}
```

A pixel is inked if its coverage exceeds the threshold at its position. Over an
area, the fraction of inked pixels approximates the grey. Note
**`rem_euclid`** rather than `%`. In Rust, `-1 % 4` is `-1`, while
`(-1).rem_euclid(4)` is `3`. Coordinates can be negative (text partly scrolled
off-screen), and an array index must not be.

Dithering is great for images but bad for small text: letter edges become
speckled. So `5c35181`, "threshold text coverage instead of dithering it", made
text use a plain threshold. Then came the reader settings for **text
darkness** (a gain on coverage, so thin fonts can be made bolder) and
**antialiasing** (keep grey edges on the panel's native grey levels). Each change
was measured with `cargo xtask text-quality`, a harness that renders reader text
exactly as the device does and reports metrics, so the effect of a setting was
visible before anyone looked at a real panel.

`TextDarkness` itself is a newtype over a percentage:

```rust
pub struct TextDarkness(u8);

impl TextDarkness {
    pub const NORMAL: Self = Self(100);

    /// coverage that paints as half ink
    const HALF: u32 = 128;

    pub const fn percent(percent: u8) -> Self {
        Self(percent)
    }
    // ...
}
```

## Hinting behind a feature

At small sizes on a low-resolution screen, outlines scaled mathematically look
blurry: a vertical stem 1.4 pixels wide straddles two pixel columns. **Hinting**
nudges outlines onto the pixel grid. Writing an autohinter is a project of its
own, so InkPaper uses `skrifa` (from Google's fontations project) behind the
`hinting` feature:

```toml
hinting = ["ttf", "dep:skrifa", "dep:spin"]
```

`spin` provides a spin-lock `Mutex` that works in `no_std` (there are no OS
threads to block, so waiting means spinning). `HintedTtfFont` uses one for
**interior mutability**: the `FontFace` methods take `&self`, but the hinted font
wants to build caches lazily on first use (the parsed outlines, the most recent
instances). `state: spin::Mutex<HintingState<'a>>` lets a `&self` method mutate
them safely, the same role `RefCell` played in chapter 4, except that a `Mutex`
is also `Sync`. The feature is on by default in the app (`default = ["hinting"]` in
`crates/app/Cargo.toml`) but off in the UI crate itself, so the framework stays
small for other users. And the `justfile` checks that the crate builds with and
without it (chapter 1).

## Rust concepts in this chapter

- **Implementing a library's visitor trait** (`ttf_parser::OutlineBuilder`).
- **Closures as short-lived `FnMut` arguments** that borrow `self`.
- **Fixed-capacity algorithms** with explicit overflow; insertion sort on arrays.
- **Floats in `no_std`**: `libm`, saturating `as` casts from floats, containing lint
  exceptions in one module.
- **Const generics for cache sizes**; `#[cfg]` on struct fields.
- **`wrapping_*` arithmetic** for hashing; linear probing.
- **Borrowed results** (`GlyphBitmap<'_>`) and how `&mut self` prevents use after
  overwrite.
- **Revision counters** for lazy invalidation.
- **`rem_euclid` vs `%`** for negative numbers.
- **Optional heavy features** (`hinting`) and `no_std` spin locks.

## Try it yourself

1. With the `metrics` feature, run the `glyph_hinting` or `rendering` benches in
   `crates/ui/benches` and look at the glyph cache hit rate. Then halve the
   cache's `SLOTS` and compare.
2. Write a test that calls `get_or_rasterize` twice and tries to keep the first
   `GlyphBitmap` alive across the second call. Read the borrow checker error and
   explain what bug it prevents.
3. Print `bayer_4x4_threshold(x, y)` for `x, y` in `-4..4`. Then replace
   `rem_euclid` with `%` and see what breaks.
