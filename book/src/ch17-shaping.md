# 17. When text is more than letters

Imagine a paragraph that quotes an Arabic name, includes the number 12, then returns to English. The reader should recognize the name and the number without thinking about which direction the software draws. A loop that paints each character and moves right cannot deliver that experience. Reversing the whole string is worse: it reverses the digits too.

The missing step is **shaping**: choosing glyphs, grouping related source characters, ordering the result for display and positioning it. InkPaper implements a compact shaper with specific capabilities and explicit limits. Understanding those limits is part of learning the implementation, not an apology at the end. Valid Unicode text, complete font coverage and correct typography are three different promises.

## Four ways to look at the same word

Rust's `&str` contains valid UTF-8. Its length counts bytes; `.chars()` produces Unicode scalar values. Neither directly counts the units a reader perceives. An accented letter may be one precomposed scalar or a base followed by a combining mark. A glyph is yet another unit: one glyph may represent several scalars, or several glyphs may form one visible unit.

This is a complete standalone Rust example, written for this session rather than copied from production code:

```rust
fn main() {
    for text in ["café", "cafe\u{301}"] {
        println!("{} bytes, {} scalars", text.len(), text.chars().count());
    }
    assert_eq!("café".get(0..4), None);
    assert_eq!("café".get(0..3), Some("caf"));
}
```

It prints five bytes and four scalars for the first spelling, six bytes and five scalars for the second. Byte four lies inside the two-byte `é`, so that range is not a valid string slice. `get` returns `None`; direct indexing with the same range would panic. A scalar boundary is necessary for slicing, but still does not guarantee a good place to break a visible letter from its accent.

The shaper records a **cluster** on each `ShapedGlyph`: a UTF-8 byte offset into its source piece. Several glyphs can share that offset. A lam-alef ligature can represent two source scalars with one glyph; a base and its marks can require several glyphs with one cluster. These offsets preserve a connection to logical text after the painted order changes.

Do not equate these shaping clusters with a complete implementation of Unicode extended grapheme segmentation. `marks.rs` groups characters classified as `Extend` or `SpacingMark` with a preceding cluster, and the logical shaper has special handling for Arabic and selected ignorables. That is useful behavior, but not every grapheme rule or script-specific shaping rule. Recognizing that an Indic vowel sign extends a cluster, for example, does not implement an Indic shaping engine.

## Put the number in the right direction

Text is stored in logical reading order. Display order can differ. InkPaper divides a line into directional runs, resolves their levels, and visits them in visual order. Within a right-to-left run it reverses clusters, not the internal order of a base and its marks. Numbers can therefore retain their left-to-right sequence inside a right-to-left line.

Unicode property tables supply the raw character information. The exact classifier in `crates/ui/src/text_shaping/bidi.rs` shows how much of that information this algorithm uses:

```rust,ignore
fn directional_class(character: char) -> DirectionalClass {
    match BIDI_CLASSES.get(character) {
        BidiClass::LeftToRight => DirectionalClass::LeftToRight,
        BidiClass::RightToLeft | BidiClass::ArabicLetter => DirectionalClass::RightToLeft,
        BidiClass::EuropeanNumber | BidiClass::ArabicNumber => DirectionalClass::Number,
        _ => DirectionalClass::Neutral,
    }
}
```

Mapping an external enum to a smaller internal one makes later decisions easier to follow. It also discards distinctions. This is not the full Unicode Bidirectional Algorithm merely because the source data comes from ICU. In particular, the four-way classification does not implement the complete explicit embedding, override and isolate machinery. The line's initial direction comes from its first strong character, with left-to-right as the default; the helper's name, `paragraph_direction`, does not mean the caller passes an entire multi-line paragraph.

Bracket pairing and mirroring improve mixed-direction punctuation. Their storage is bounded: there are 32 directional-run slots and 32 bracket-pair slots. Too many directional runs produces `ShapeError::TooManyDirectionalRuns`. If bracket analysis exceeds its capacity, the builder discards that pairing analysis and treats brackets as ordinary neutrals. Those are different policies—one reports failure, the other degrades behavior—and neither should be described as unlimited bidi support.

For an ordinary left-to-right line without right-to-left characters, the builder uses a single-run fast path. It avoids spending a run on every alternating word, number and punctuation mark. The optimization follows the common reading experience without changing the public representation.

## Let neighbors change a letter

Arabic adds another layer: adjacent letters can join, and a letter's shape depends on whether it connects before and after itself. The logical shaper maintains joining state separately from the previously rendered glyph. That matters because a transparent mark may produce a glyph without interrupting the joining chain. Zero-width joiner and non-joiner also affect this state even though they draw no glyph of their own.

The implementation consults Arabic contextual GSUB features when available, with presentation forms as a compatibility fallback. Its explicitly recognized ligature is adjacent lam-alef. It prefers the font's required-ligature substitution and can fall back to a presentation-form glyph; if no suitable ligature is available, it does not consume the alef as though the combined glyph existed. This is narrower than a general GSUB engine that executes arbitrary shaping programs for every script.

Combining marks need positioning as well as grouping. The shaper uses supported font anchors and fallback placement rules for above-, below- and centered marks, with special handling for shadda. But a mark for which it has no placement rule can retain its own advance. “All combining marks have zero advance” would be an attractive simplification and an incorrect description of this implementation.

Positioning happens after visual ordering, so kerning and other supported pair adjustments operate on the resulting neighbors. `ShapedGlyph` keeps `base_advance` separately from its positioned `advance` and `offset`. This avoids trying to reconstruct the original font spacing from an already adjusted result when rebuilding visual positioning. The extra field records a distinct fact, rather than merely caching a calculation.

A mature general-purpose shaping library offers substantially broader typography and conformance. The compact implementation instead exposes a smaller working set of algorithms and memory limits. The tradeoff is not “Unicode versus no Unicode”; it is which Unicode behaviors and font features the product actually implements.

## Stream glyphs, preserve whole clusters

A long line need not require a vector containing every glyph. The production API visits each result through a callback. Here is the exact method from `crates/ui/src/text_shaping/line.rs`, in its existing `PreparedSimpleShaper` implementation:

```rust,ignore
pub fn shape_line_with<const FONTS: usize>(
    &self,
    registry: &'font FontRegistry<'_, FONTS>,
    size_px: u16,
    text: &str,
    suffix: &str,
    visit: impl FnMut(ShapedGlyph),
) -> Result<ShapeSummary, ShapeError> {
    self.shape_line_in_chunks::<LINE_CHUNK_GLYPHS, FONTS, 1>(
        registry, size_px, text, suffix, None, visit,
    )
}
```

`FnMut` permits a caller to update state: a painter can draw, a test can collect glyphs, and a measurer can ignore individual results and use the returned summary. The callback avoids forcing every consumer to allocate a collection. The optional suffix is useful for an ellipsis, processed with the line rather than simply painted at a guessed edge; emitted suffix clusters are relative to that suffix piece.

The glyph chunk holds 64 entries. Whole clusters stay together, and positioning state carries across chunks. A single cluster that cannot fit reports `BufferTooSmall`. The directional-run limit still applies, so “a line of any length” describes freedom from a total-glyph-count buffer, not permission for arbitrary complexity.

Memory is traded for work. Mixed-direction analysis can shape without retaining the glyphs, then shape again to emit runs. Right-to-left runs use repeated passes to fill chunks from the end. This avoids a whole-line glyph allocation but can cost more computation for long right-to-left input. A caller that collects every callback result into a `Vec` also deliberately gives up that storage advantage.

Line breaking is a related but separate decision. The reader uses `unicode-linebreak` for potential break positions and measures what fits. Its font adapter asks the shaper for cluster boundaries when splitting further, avoiding cuts through the groups this shaper recognizes. Neither Unicode break opportunities nor correct byte boundaries alone guarantee correct rendering for an unsupported script.

## Trace four clusters without a screen

Optional: open `arabic_with_numbers_keeps_digits_in_ltr_order` in `crates/ui/src/text_shaping/tests.rs`. Its input is `"ب 12"`. The letter occupies bytes zero and one; the space begins at two, then the digits at three and four. Predict visual order before reading these exact assertions from that test:

```rust,ignore
assert_eq!(run.glyphs()[0].cluster(), 3);
assert_eq!(run.glyphs()[1].cluster(), 4);
assert_eq!(run.glyphs()[2].cluster(), 2);
assert_eq!(run.glyphs()[3].cluster(), 0);
```

`cargo test -p inkpaper-ui arabic_with_numbers_keeps_digits_in_ltr_order` runs the existing check. The expected visual storage is `1`, `2`, space, then the Arabic letter—not a reversal of every character. The test's `shape_line` method is a local helper that collects the streaming API into a vector; it is not an additional public production API. Reading that helper is a useful final connection: the same callback that keeps production storage bounded also makes exact glyph-order assertions straightforward.
