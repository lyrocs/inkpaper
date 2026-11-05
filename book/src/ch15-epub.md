# 15. Opening a book, not just a file

You tap a book cover. What should happen next is wonderfully uninteresting: the title appears, the first chapter opens, and turning the page continues the story. Yet the file does not contain a single stream of pages. It contains a small collection of documents, styles and pictures, with instructions for assembling them.

An EPUB reader's first task is therefore not drawing. It is discovering which resources belong to the book, which order to read them in, and how much work to do now. InkPaper's `inkpaper-epub` crate makes those decisions without knowing anything about the screen. Following that boundary is a useful Rust lesson: a modest interface can separate a complicated format from an equally complicated device.

## A package has two different maps

An EPUB is a ZIP archive. Inside it, `META-INF/container.xml` identifies the package document, usually an `.opf` file. The package's **manifest** names resources and their media types. Its **spine** lists references to manifest items in reading order. These are different jobs: an alphabetically sorted list of archive filenames cannot tell us whether the introduction comes before chapter one.

The table of contents is a third view, intended for navigation rather than exhaustive reading order. InkPaper can load an EPUB 3 navigation document or an EPUB 2 NCX document. A book may open without one: `load_navigation` returns `Result<Option<Navigation>, _>`, distinguishing an absent navigation resource from a failure while reading it.

The opening method in `crates/epub/src/lib.rs` is short enough to read in full. This is an exact method excerpt; it belongs inside the existing `impl`, not in a standalone program.

```rust,ignore
pub async fn open(source: S) -> Result<Self, Error<S::Error>> {
    let mut archive = Archive::open(source).await?;

    let container_path = ArchivePath::new("META-INF/container.xml")
        .expect("the EPUB container path is statically valid");

    let container_bytes = archive.read_entry(&container_path).await?;
    let container_xml = core::str::from_utf8(&container_bytes).map_err(Error::Utf8)?;
    let container = parse_container(container_xml).map_err(Error::Container)?;
    let package_bytes = archive.read_entry(container.package_path()).await?;
    let package_xml = core::str::from_utf8(&package_bytes).map_err(Error::Utf8)?;
    let package =
        parse_package(package_xml, container.package_path().clone()).map_err(Error::Package)?;

    Ok(Self { archive, package })
}
```

Notice what it does **not** do: parse every chapter or decode every image. Opening discovers the package; subsequent calls request content. A successful `open` is not a promise that every later resource is valid. That distinction lets a reader do useful work before loading an entire book, but it also means a later page turn may encounter a chapter error.

`from_utf8` checks the bytes and lends the parser a `&str`; it does not copy the document. The parser then builds the owned package information that must survive those temporary buffers. The `?` operators preserve failure, while `map_err` identifies its stage. A bad source read and malformed package XML should not become the same mysterious “book failed” internally.

## Ask for bytes, not a filesystem

ZIP's resource directory lives near the end of the archive. The reader needs random access, but it does not need directories, file creation or an operating-system file handle. Its complete source trait, from `crates/epub/src/source.rs`, expresses just that requirement:

```rust,ignore
#[allow(async_fn_in_trait)]
#[allow(clippy::len_without_is_empty)]
pub trait EpubSource {
    type Error;

    async fn len(&mut self) -> Result<u64, Self::Error>;

    async fn read_exact_at(&mut self, offset: u64, buffer: &mut [u8]) -> Result<(), Self::Error>;
}
```

`type Error` lets each source retain its own failures. `Epub<S>` is generic over a concrete source, so archive code can await reads without depending on one storage implementation. Taking `&mut self` also permits sources with mutable I/O state; it does not claim that fetching bytes is a pure operation.

For tests, `SliceSource` borrows an existing byte slice. Its implementation converts the requested `u64` offset with `usize::try_from`, adds the buffer length with `checked_add`, and uses `get(start..end)` before copying. Those three operations protect different boundaries: the target's address width, arithmetic overflow, and the actual slice length. Rust does not make file-provided numbers trustworthy; it gives the implementation convenient ways to reject them.

The archive reads at most 22 plus 65,535 bytes while searching for the end-of-central-directory record: the minimum record length plus the format's maximum comment length. A matching signature is accepted only when its recorded comment ends at the archive's end. The archive retains directory coordinates rather than a full in-memory index, and searches entries when requested. That saves retained metadata at the cost of repeated directory reads. An eagerly indexed ZIP library would make a different, perfectly reasonable tradeoff on a machine with more memory.

## Bounded work is not the same as a memory budget

Before reading an entry, `archive.rs` checks offset arithmetic and ensures the compressed payload fits inside the archive. It supports stored and deflated entries, and explicitly rejects features such as encryption, ZIP64 and multi-disk archives. Rejecting an unsupported format is preferable to interpreting its fields as something simpler.

Here is the exact deflate arm from `read_entry`:

```rust,ignore
COMPRESSION_DEFLATE => {
    let decompressed = decompress_to_vec_with_limit(&compressed, expected_len)
        .map_err(|_| Error::Archive(ArchiveError::Inflate))?;

    if decompressed.len() != expected_len {
        return Err(Error::Archive(ArchiveError::SizeMismatch {
            expected: entry.uncompressed_size,
            actual: decompressed.len(),
        }));
    }

    Ok(decompressed)
}
```

The limit prevents output from exceeding the entry's declared uncompressed size. The following check also rejects output that is shorter than promised. Both matter: a parser should not quietly accept a different resource from the one its metadata describes.

But `expected_len` comes from the archive. This is **not** a device-wide, trusted maximum. The implementation allocates the compressed entry and produces an owned decompressed vector; a large declared size can still demand substantial memory. Likewise, loading a chapter materializes its text and blocks. Bounded searches and checked arithmetic are valuable protections, not evidence that every valid or hostile EPUB will fit the device. Keeping that distinction clear is more useful than calling a parser “safe” without saying safe from what.

Resource references need a separate boundary. `ArchivePath` wraps a normalized string, rejecting absolute paths, backslashes and `..` segments that escape the archive root. Relative resolution removes the query and fragment portion when finding a resource and handles encoded path characters. A fragment still has meaning to navigation; it simply is not part of a ZIP entry's filename. The newtype makes this distinction visible to callers instead of requiring every read to repeat path validation.

## Turn document markup into reading material

`load_spine_chapter` follows the package's reading order, including supported manifest fallback chains, and loads XHTML. A fallback chain is not an invitation to loop indefinitely: the package traversal is bounded by the manifest length. A broken or cyclic chain without XHTML leaves the original item selected, which chapter loading can reject as unsupported content.

XHTML becomes blocks and inline content, with style nodes and image references. Images are not embedded decoded bitmaps in every paragraph. Their resource paths let later stages ask for dimensions or bytes when needed. This is a reading model, not a browser DOM with scripting and arbitrary layout.

Styles are similarly selective. InkPaper resolves properties such as weight, italic style, alignment, vertical margins, indentation, line height and hidden content. External CSS must be declared as `text/css` in the manifest. The supported selector and property rules are deliberately narrower than a browser's; an EPUB relying on other CSS may not reproduce its publisher's layout.

A small Rust detail carries a surprisingly important rule. This exact declaration is from `crates/epub/src/css/parser.rs`:

```rust,ignore
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct Specificity {
    ids: u16,
    classes: u16,
    elements: u16,
}
```

Derived ordering compares fields in declaration order. Thus an ID count wins before class count, and class count before element count. The surrounding cascade also considers importance and rule order. Reordering these fields for cosmetic reasons would change style selection: the data representation is part of the algorithm.

By the end of loading, the reader has something much more useful than XML tokens: ordered content with resolved reading styles. The next questions—what a letter looks like and how many words fit—belong to fonts and layout, not the archive.

## Try one package detour

Optional, without hardware: read `follows_manifest_fallback_for_unsupported_spine_resource` in `crates/epub/tests/compatibility_fixtures.rs`. It opens the checked-in `fixtures/compat-manifest-fallback.epub` with `SliceSource`, then requests spine entry zero. Trace `Package::spine_content_item` to predict which resource it selects.

Run `cargo test -p inkpaper-epub --test compatibility_fixtures follows_manifest_fallback_for_unsupported_spine_resource` if you want the executable check. The existing assertions expect `OEBPS/chapter.xhtml` and text containing “InkPaper should render this XHTML document”. The observation is not merely that a ZIP opens: the package's fallback relation changes what the reader receives, without changing its request for the first reading-order item.
