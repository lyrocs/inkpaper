# 16. Giving letters their ink

A page can contain exactly the right words and still be tiring to read. Thin strokes disappear, small curves look ragged, or a larger font makes every page turn hesitate. Typography is not decoration added after the reader works. It is part of whether the reader works comfortably at all.

InkPaper separates a letter's shape from the pixels used to show it. That separation lets the same font serve different sizes, lets repeated letters reuse work, and lets text darkness change without changing line breaks. This session follows one glyph from a font face into a reusable coverage bitmap, then asks what the Rust types protect along the way.

## A glyph is a shape, not a character

The character `A` is a Unicode value. A glyph is an entry in a particular font: it has an identifier, metrics and a drawable shape. Another font's glyph with the same numeric identifier may depict something entirely different. Nor is the relationship always one character to one glyph. Chapter 17 will handle combinations and contextual forms; here we concentrate on drawing the chosen result.

`FontFace`, in `crates/ui/src/font/mod.rs`, lets bitmap and scalable fonts provide the same services. These are two exact, non-adjacent method declarations from that trait, shown together; other methods are omitted:

```rust,ignore
fn glyph_id(&self, character: char) -> Option<GlyphId>;

fn rasterize(
    &self,
    glyph: GlyphId,
    size_px: u16,
    coverage: &mut [u8],
) -> Result<(), FontRasterError>;
```

The `Option` is an important admission: no face is required to contain every character. Choosing another face or a replacement is the registry and shaping layer's responsibility. Font coverage—the repertoire a face contains—is different from pixel coverage, the image that `rasterize` writes.

The mutable slice belongs to the caller. Rasterization does not return a freshly allocated bitmap; the caller supplies storage of the required size. Font metrics describe the bitmap's bounds and bearings, while the advance describes how far the writing pen moves. A space can advance without painting a visible shape. An italic glyph can extend beyond its advance. Treating bitmap width as spacing would make both cases wrong.

Scalable faces use `ttf-parser` to inspect font data. Keeping those bytes alive is essential: a borrowed parser view is not an owned copy of the font. The interface also separates inexpensive advance lookup from computing raster bounds, because measuring a paragraph should not require painting it first.

A collection of pre-rendered bitmap sizes would simplify this pipeline and could be excellent for a tiny fixed interface. A reading app needs more flexibility. Scalable outlines move the cost from shipping every size to evaluating a shape at the size the reader chooses.

## Curves become partial pixels

A font outline describes contours with lines and curves. At screen size, an edge rarely lands exactly between pixel columns. Rasterization estimates how much of each pixel lies inside the outline, producing a byte from zero to 255. These bytes are **coverage**, not a promise that the display can show 256 gray levels.

The scanline rasterizer in `crates/ui/src/font/ttf/raster.rs` uses four horizontal and four vertical samples per pixel. Curves are flattened into line segments; for each sample row, the rasterizer records where segments cross it. This is the exact crossing calculation:

```rust,ignore
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

The half-open comparisons include one endpoint but exclude the other, avoiding double-counting shared vertices. Direction contributes either positive or negative winding. Once crossings are sorted horizontally, a nonzero winding sum identifies the inside of the shape, including contours with holes. Counting covered samples then approximates the edge's coverage.

This is a consciously finite approximation. Curve subdivision has a cap, and a scanline can hold at most 96 intersections. Overflow is recorded and reported as a rasterization error rather than silently growing an edge vector. That bounds temporary crossing storage; it does not mean every glyph bitmap is tiny or every font subsystem is allocation-free.

Hinting addresses a different problem. A mathematically scaled stem may straddle the pixel grid awkwardly at small sizes. The optional `hinting` feature uses `skrifa` to adjust outlines for the grid. The app enables this feature by default, while the UI crate does not. Hinted faces maintain their own cached state; this is not simply the basic rasterizer with a different rounding function.

Importantly, the current hinted implementation keeps unhinted advances. The test `hinting_keeps_unhinted_advances` checks that contract: adjusting how a stroke meets the grid need not change how far text measures. Hinting can improve small text, but its extra machinery and caches are a real cost, not a free quality switch.

## Store the work that repeats

A page reuses many glyphs. Rasterizing each occurrence of `e` independently would repeatedly solve the same geometry problem. InkPaper stores coverage in a fixed byte arena and keeps a fixed table pointing into it. This is the exact cache declaration from `crates/ui/src/font/cache.rs`:

```rust,ignore
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

These const generics express two independent budgets. Many small glyphs can fill the slot table while leaving bytes free. A few large glyphs can fill the byte arena first. If either resource runs out, the cache clears the whole generation and inserts the new glyph. A single glyph larger than the entire arena is rejected instead.

The key includes font instance, weight, glyph identifier and pixel size. Darkness and antialiasing are absent because they are applied when painting, not baked into cached coverage. A font registry revision also matters: an identifier can be reused after an owned face is removed. On a revision mismatch the cache clears, preventing an old face's pixels from masquerading as the new one.

Lookup uses a hash followed by linear probing. If the home slot is occupied by another key, it checks successive slots. Finding an empty slot proves the key is absent **because individual entries are never deleted**. Selective deletion would require preserving the probe chain, for example with tombstones. The simpler whole-cache policy keeps this invariant easy to maintain, though clearing discards useful glyphs and can cause bursts of repeated rasterization. An LRU cache could retain a better working set at the cost of bookkeeping and more complicated storage reclamation.

The return type is as interesting as the lookup. This exact signature excerpt omits the method body:

```rust,ignore
pub fn get_or_rasterize<const FONTS: usize>(
    &mut self,
    registry: &FontRegistry<'_, FONTS>,
    font: impl Into<FontInstance>,
    glyph: GlyphId,
    size_px: u16,
) -> Result<GlyphBitmap<'_>, GlyphCacheError>
```

`GlyphBitmap<'_>` borrows the cache's storage. While the caller still needs that view, it cannot mutably borrow the same cache for another insertion that might clear or overwrite it. Draw this glyph, finish using the borrow, then request the next. Rust enforces the relationship without copying the bitmap or maintaining runtime reference counts. The restriction is useful precisely because the storage is reusable.

## Coverage is not yet ink

On e-paper, an edge sample must eventually become one of the panel's supported tones. The UI's glyph view carries darkness and antialiasing preferences alongside coverage; the painter decides how to use them. A thresholded text path makes a hard black-or-white decision, while a gray-capable path can retain edge tones. Ordered dithering is another possible approximation, but its visible dot pattern is a different aesthetic from a smooth letter edge.

Text darkness is not a font weight substitution. `TextDarkness::apply`, in `crates/ui/src/style/text.rs`, remaps coverage with two straight segments around a movable midpoint. Empty pixels stay empty and fully covered pixels stay fully inked. Increasing darkness moves where an edge reaches half ink, making marginal stroke pixels more likely to appear. Choosing a heavier variable-font weight instead changes the font instance and therefore the cache key.

That distinction explains a pleasant product behavior: a paint-only adjustment can reuse both glyph bitmaps and page geometry. It also explains a limitation. Darkening cannot invent a missing glyph, fix incorrect shaping, or recover detail that the selected rasterization size never represented. Those are separate layers, and keeping them separate makes quality problems much easier to reason about.

## Watch a tiny cache fill

Optional, without hardware: trace `glyph_cache_flushes_when_storage_is_full` in `crates/ui/src/font/tests.rs`. Its test font paints four-byte glyphs into `GlyphCache::<4, 6>`. After inserting `A`, four bytes are used. Predict the usage after requesting `B` before reading the final assertion.

The expected result is four bytes again, not eight: `B` cannot fit beside `A`, so the cache starts a fresh generation. `cargo test -p inkpaper-ui glyph_cache_flushes_when_storage_is_full` runs that existing check. For comparison, `glyph_cache_returns_cached_coverage` requests the same glyph twice and expects the arena usage to stay at four bytes without needing a new generation. Neither test depends on a real font file or display; they isolate the cache policy from typography.
