# 17. Shaping text: Unicode, bidi and Arabic

> **Where we are:** 30–31 August 2026, an intense two days: `b0c1c04` "add text
> shaping foundation", `0afc618` "add arabic contextual shaping and lam-alef
> ligatures", `ce75b04` "add rtl text ordering", `d7538eb` mixed-direction runs,
> GPOS/GSUB support (`6f2a3ec` … `65fcabd`). Then in October, `06f770f` and
> `02b7aa2` moved character data to `icu_properties`, `73d884b`/`d40815a`
> adopted Unicode line breaking, and `9103b6d` (29 October) let lines of any
> length shape through a fixed buffer.

## Text is harder than it looks

"Draw the string" sounds like a loop over characters: look up each glyph, draw
it, move right by its width. That works for English set in a simple font. It
fails for:

- **Kerning**: "AV" looks better with the V tucked under the A.
- **Ligatures**: in Arabic, lam followed by alef *must* become a single combined
  glyph.
- **Contextual forms**: an Arabic letter has up to four shapes (isolated,
  initial, medial, final), depending on its neighbours.
- **Combining marks**: vowel signs sit above or below the letter before them, at
  zero advance.
- **Bidirectional text**: Hebrew and Arabic run right to left, and their numbers
  run left to right inside them. A line mixing English and Arabic has to be
  reordered for display.

**Shaping** is the process that turns a string into positioned glyphs. Full
shapers like HarfBuzz are huge. InkPaper wrote a compact one covering Latin,
Hebrew and Arabic. This chapter isn't about the typography itself. It's about
how Rust's view of strings shapes the code.

## Bytes, chars and clusters

A Rust `&str` is guaranteed-valid UTF-8 bytes. There are three ways to count it:

| Unit | Rust | `"é"` (precomposed) | `"é"` (e + combining accent) |
|------|------|---------------------|------------------------------|
| bytes | `s.len()` | 2 | 3 |
| chars (Unicode scalar values) | `s.chars().count()` | 1 | 2 |
| what a reader sees | (needs Unicode data) | 1 | 1 |

Rust deliberately doesn't let you index a string by character: `s[3]` doesn't
compile, because there's no O(1) way to find the 3rd `char` in UTF-8. You can
slice by *byte* range, `&s[a..b]`, and that **panics if `a` or `b` isn't on a
character boundary**. That's why chapter 18's code has lines like:

```rust
fn text(&self) -> &'a str {
    self.source
        .get(self.start..self.end)
        .expect("pending text range must stay on UTF-8 boundaries")
}
```

`str::get(range)` returns `None` instead of panicking, and the `expect` documents
the invariant that should make it succeed.

InkPaper's shaper tracks a **cluster** for every glyph: the byte offset of the
logical character group it came from.

```rust
pub struct ShapedGlyph {
    font: FontInstance,
    glyph: GlyphId,
    /// UTF-8 byte offset in the original input corresponding to the logical cluster that
    /// produced this glyph.
    ///
    /// multiple glyphs may share one cluster, and one glyph represents multiple
    /// source characters
    cluster: usize,
    /// unpositioned horizontal advance reported by the font.
    ///
    /// this deliberately remains separate from `advance` so bidi reordering can discard
    /// logical-order and rebuild final visual positioning without recovering the value
    /// from `offset`
    base_advance: Pixels,
    /// number of logical source components represented by this base glyph.
    ///
    /// normal bases contain one component. Marks contain zero. Our current lam-alef
    /// ligature contains 2
    ligature_components: u16,
    /// arabic combining-mark role
    mark_placement: Option<MarkPlacement>,
    /// visual position relative to the current pen
    offset: Offset,
    /// amount by which the visual pen moves after this glyph
    advance: Pixels,
}
```

Clusters are byte offsets because byte offsets are what you can *slice with*.
Selecting a word for the dictionary (the long press from chapter 10), breaking a
line, or mapping a tap to a character all need to go from a glyph back to a
`&str` range. Read the field comments: each one explains a decision, like why
two advances are kept. When a struct encodes a subtle algorithm, its field docs
are the best place to explain it.

## Unicode data at compile time

Is a character right-to-left? Does it join to its neighbours? Is it a bracket
that should be mirrored? The answers are in the Unicode Character Database.
Early versions hard-coded tables. In October (`06f770f`, `02b7aa2`) the shaper
switched to `icu_properties`, the Unicode crate from the ICU4X project:

```rust
use icu_properties::{
    CodePointMapData, CodePointMapDataBorrowed,
    props::{BidiClass, BidiMirroringGlyph, BidiPairedBracketType},
};

const BIDI_CLASSES: CodePointMapDataBorrowed<'static, BidiClass> =
    CodePointMapData::<BidiClass>::new();
const MIRRORING_GLYPHS: CodePointMapDataBorrowed<'static, BidiMirroringGlyph> =
    CodePointMapData::<BidiMirroringGlyph>::new();
```

With the `compiled_data` feature, ICU4X **bakes the data into the binary**, and
`CodePointMapData::new()` is a `const fn` returning a borrowed view of static
tables. These are `const` items, fully computed at compile time, with no
initialisation at runtime and no file to load from the SD card. Lookups are a
trie walk:

```rust
fn directional_class(character: char) -> DirectionalClass {
    match BIDI_CLASSES.get(character) {
        BidiClass::LeftToRight => DirectionalClass::LeftToRight,
        BidiClass::RightToLeft | BidiClass::ArabicLetter => DirectionalClass::RightToLeft,
        BidiClass::EuropeanNumber | BidiClass::ArabicNumber => DirectionalClass::Number,
        _ => DirectionalClass::Neutral,
    }
}
```

Unicode defines 23 bidi classes, and the shaper needs four. Mapping a big
external enum onto a small internal one, right at the boundary, keeps the rest of
the code simple: every later `match` on `DirectionalClass` has four arms, not
twenty-three. The `_` arm is a deliberate catch-all. Unicode adds categories
over time, and they'll safely count as neutral.

## Bidi in bounded memory

The paragraph's base direction comes from its first "strong" character:

```rust
/// the direction of a line's text: the direction of its first strong character
pub(crate) fn paragraph_direction(text: &str) -> TextDirection {
    for character in text.chars() {
        match directional_class(character) {
            DirectionalClass::LeftToRight => return TextDirection::LeftToRight,
            DirectionalClass::RightToLeft => return TextDirection::RightToLeft,
            DirectionalClass::Number | DirectionalClass::Neutral => {}
        }
    }

    TextDirection::LeftToRight
}
```

`return` from inside a `for` loop inside a `match`, with an empty block `{}` for
"keep looking". That's simple and early-exiting.

The line is then split into **directional runs**, stored in fixed arrays:

```rust
const DIRECTIONAL_RUN_CAPACITY: usize = 32;
/// lines with more bracket pairs, or deeper nesting, leave their brackets as plain neutrals
const BRACKET_PAIR_CAPACITY: usize = 32;

pub(super) struct TextRunBuilder<'text> {
    text: &'text str,
    paragraph_direction: TextDirection,
    /// without right-to-left text every glyph resolves to level 0, so the text is one
    /// run however often words, numbers and punctuation alternate
    single_run: bool,
    brackets: [BracketPair; BRACKET_PAIR_CAPACITY],
    bracket_count: usize,
    runs: [TextRun; DIRECTIONAL_RUN_CAPACITY],
    written: usize,
}
```

Again there's a fixed capacity with a documented degradation: too many bracket
pairs, and the extras are just treated as neutral characters. The text renders
slightly less perfectly, and nothing crashes or allocates. And note the
`single_run` fast path: for a purely left-to-right line (most of a typical
book) all the bidi machinery is skipped.

`TextRunBuilder<'text>` borrows the line's text. The builder can't outlive the
string it's analysing, and that's checked at compile time.

Helpers that look backwards and forwards through runs use slices and reverse
iterators:

```rust
fn previous_strong_direction(runs: &[TextRun], index: usize) -> Option<TextDirection> {
    for run in runs[..index].iter().rev() {
        match run.class {
            DirectionalClass::LeftToRight => return Some(TextDirection::LeftToRight),
            DirectionalClass::RightToLeft => return Some(TextDirection::RightToLeft),
            DirectionalClass::Number | DirectionalClass::Neutral => {}
        }
    }

    None
}
```

`runs[..index]` is the sub-slice before `index`, and `.iter().rev()` walks it
backwards. There's no index arithmetic to get wrong.

## Arabic: four shapes per letter

Whether an Arabic letter joins its neighbours depends on its **joining type**,
again from Unicode data, mapped onto a small internal enum with helper methods:

```rust
impl JoiningType {
    /// whether this character can connect to the preceding logical arabic character.
    pub(crate) const fn accepts_previous(self) -> bool {
        matches!(
            self,
            Self::JoinCausing | Self::DualJoining | Self::RightJoining
        )
    }

    /// whether this character can continue a joining chain toward the following logical
    /// arabic character
    pub(crate) const fn connects_forward(self) -> bool {
        matches!(
            self,
            Self::JoinCausing | Self::DualJoining | Self::LeftJoining
        )
    }
}
```

Once the shaper knows whether a letter joins backwards and forwards, picking its
form is a `match` on a **tuple of booleans**:

```rust
fn presentation(self, joins_previous: bool, joins_next: bool) -> Option<char> {
    let codepoint = match (joins_previous, joins_next) {
        (false, false) => self.isolated,
        (true, false) => self.final_form,
        (false, true) => self.initial,
        (true, true) => self.medial,
    };

    if codepoint == 0 {
        return None;
    }

    char::from_u32(u32::from(codepoint))
}
```

A tuple of two `bool`s has exactly four values, so the compiler checks that the
`match` covers all four. It reads like the table in the Unicode standard. Missing
forms are stored as `0`, so the function returns `None` and the shaper keeps the
base letter. Storing the table as `u16` instead of `char` halves its size, since
every Arabic presentation form fits in 16 bits. `char::from_u32` converts back
safely.

Finding a letter's forms is a **binary search** over a sorted `const` table:

```rust
fn forms_for(character: char) -> Option<ArabicForms> {
    let codepoint = u16::try_from(character as u32).ok()?;

    let mut low = 0usize;
    let mut high = ARABIC_FORMS.len();

    while low < high {
        let middle = low + (high - low) / 2;
        let candidate = ARABIC_FORMS[middle];

        if candidate.base < codepoint {
            low = middle + 1;
        } else if candidate.base > codepoint {
            high = middle;
        } else {
            return Some(candidate);
        }
    }

    None
}
```

`u16::try_from(character as u32).ok()?` handles characters outside the 16-bit
range by returning `None` early: they can't be Arabic letters. `low + (high -
low) / 2` is the overflow-safe midpoint, a classic. (The standard library's
`slice::binary_search_by_key` would also work here. Hand-writing it keeps the
function `const`-friendly and explicit.)

Then fonts add their own rules. **GSUB** (glyph substitution) tables provide the
contextual forms and ligatures, and **GPOS** (glyph positioning) provides kerning,
mark attachment and cursive joining. Commits `c55f556` ("apply kerning after bidi
ordering") and `643baab` ("prefer gpos pair kerning") are about getting the
*order* right, because kerning must apply to visually adjacent glyphs, which in
mixed text aren't logically adjacent. That's what the separate `base_advance`
field is for.

## Lines of any length through a fixed buffer

Shaping needs somewhere to put glyphs. Early versions used a fixed array of a few
hundred and failed on longer lines (`512c664`, "fix long lines failing to shape").
The final design, from `9103b6d`, is described in the module doc:

```rust
//! shapes a line of any length through a fixed chunk of glyphs.
//!
//! a line's directional runs come from one pass over its glyphs, which keeps none of
//! them. Each run then shapes again, in visual order: a left-to-right run streams
//! forward through the chunk, and a right-to-left run fills the chunk from its end, one
//! pass per chunk. Chunks always hold whole clusters, so a run positions the same
//! whether it fits one chunk or many.

/// the most glyphs a line holds at once. A line may have any number, but each cluster,
/// a base and its marks, must fit
const LINE_CHUNK_GLYPHS: usize = 64;
```

And the API takes a **visitor closure** instead of returning a collection:

```rust
pub fn shape_line_with<const FONTS: usize>(
    &self,
    registry: &'font FontRegistry<'_, FONTS>,
    size_px: u16,
    text: &str,
    suffix: &str,
    visit: impl FnMut(ShapedGlyph),
) -> Result<ShapeSummary, ShapeError>
```

The caller (a painter, a measurer) receives each positioned glyph through
`visit` and handles it immediately. No `Vec<ShapedGlyph>` is ever built, so
memory use is constant whatever the line length. Pushing results into a callback
is the standard alternative to returning an allocated collection, and it often
beats implementing `Iterator`, which would require the shaper to suspend in the
middle of a chunk. The commit right after (`ede94b6`, "drop the buffer-based
shaping API") removed the old interface once nothing used it.

`suffix: &str` exists for ellipsis: a truncated title is shaped as "text" +
"…" in one pass, so the ellipsis is kerned and ordered correctly in RTL text too.

## Line breaking

Where may a line break? Not just at spaces. After a hyphen, between two Chinese
characters, but not before a closing parenthesis. Unicode Annex #14 defines the
rules, and the `unicode-linebreak` crate implements them:

```rust
for (segment_end, opportunity) in linebreaks(paragraph) {
    let start = segment_start;
    segment_start = segment_end;
    // ... does &paragraph[line_start..segment_end] still fit? ...
}
```

`linebreaks` returns an iterator of `(byte_offset, BreakOpportunity)`, where the
opportunity is `Mandatory` (a newline) or `Allowed`. The layout code tries each
opportunity and keeps the last one that fits. Because offsets are bytes,
`&paragraph[a..b]` slices are always on character boundaries: the iterator only
returns boundaries, so the slicing can't panic.

Both the UI (`73d884b`) and the reader's paginator (`d40815a`) use the same crate.
When two components need the same rules, sharing a well-tested crate beats two
home-made approximations.

## Rust concepts in this chapter

- **`str` is UTF-8 bytes**: `len` vs `chars().count()`, no character indexing,
  byte slicing must hit boundaries, `str::get` vs `&s[a..b]`.
- **Byte offsets as stable references into text** (clusters).
- **Compile-time data tables** (`const` items from `const fn`), `icu_properties`
  with baked data.
- **Mapping large external enums onto small internal ones** at the boundary.
- **Fixed-capacity arrays with documented degradation**; fast paths.
- **Lifetimes on analysers** (`TextRunBuilder<'text>`).
- **Slices and reverse iteration** (`runs[..index].iter().rev()`).
- **Matching on tuples of `bool`** for exhaustive decision tables.
- **Binary search**, overflow-safe midpoints, `try_from(...).ok()?`.
- **Visitor closures** (`impl FnMut(T)`) instead of returning collections.
- **Iterator-producing crates** (`unicode-linebreak`) for standard algorithms.

## Try it yourself

1. In a scratch program, print `len()`, `chars().count()` and
   `char_indices().collect::<Vec<_>>()` for `"café"`, `"cafe\u{301}"` and an
   Arabic word such as `"سلام"`.
2. Try `&"café"[0..4]` and read the panic message. Then use `get(0..4)`.
3. In `crates/ui/src/text_shaping/tests.rs`, find a test with mixed Arabic and
   Latin. Change the text and predict the visual order of the glyph clusters
   before running it.
4. Print `unicode_linebreak::linebreaks("Hello, world! (A test)").collect::<Vec<_>>()`
   and explain each offset.
