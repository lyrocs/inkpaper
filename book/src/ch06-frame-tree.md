# 6. Giving the screen a memory

Open a reader's settings drawer, scroll its list, and change one option. You want the new choice to appear without the list jumping back to the top. The application may describe its controls again, but to your finger this is still the same drawer. A useful UI therefore needs two different kinds of memory: a working description of what is on screen, and an identity for the parts whose interaction state should survive change.

InkPaper's frame connects those needs. It turns the nested element values returned by `render` into a uniform tree that layout, painting, and input can share. The frame persists between runtime calls; it is not a fresh collection of pixels. Its nodes can be retained, replaced, or compacted while identified elements keep their meaning.

This session follows that translation. The source excerpts are internal definitions or function bodies, not standalone programs.

## A description becomes working storage

An element tree is pleasant to write because its Rust types describe its composition. A container with one text child and a container with several different children need not have the same concrete type. That is useful while constructing a UI, but awkward for the next job: visit every object, assign rectangles, and later find the object under a tap.

Mounting converts those varied values into frame nodes. All nodes have one Rust type; a `NodeKind` enum distinguishes containers, text, images, SVGs, canvases, and entity boundaries. Unlike an open trait that application code can implement, this enum is a closed vocabulary of operations the framework understands. Layout and painting can match on it exhaustively. An application can invent another component without inventing another fundamental paint operation.

Here is the beginning of `Node` in `crates/ui/src/frame/mod.rs`, an exact excerpt with its remaining fields omitted:

```rust,ignore
pub(crate) struct Node {
    pub(crate) kind: NodeKind,
    pub(crate) parent: Option<NodeId>,
    pub(crate) first_child: Option<NodeId>,
    pub(crate) last_child: Option<NodeId>,
    pub(crate) next_sibling: Option<NodeId>,
    pub(crate) element_id: Option<ElementId>,
    pub(crate) element_state_id: Option<ElementStateId>,
    pub(crate) interaction: NodeInteraction,
    pub(crate) layout: NodeLayout,
```

The links are indices, not references into independently allocated nodes. `NodeId` wraps a `u16`; the arena owns the storage. This avoids a network of shared owners and mutable reference-counted cells merely to represent parent and child links. It also makes storage policy separable from tree structure: fixed-capacity and heap-backed storage can serve the same representation.

There is a cost. A numeric link does not prove that it points to the right node. The frame implementation must maintain its invariants. `append_child` requires an unattached child, rejects self-parenting with a debug assertion, and preserves sibling append order. `first_child` and `next_sibling` provide the walk; `last_child` makes appending possible without scanning every existing child. Tree order is not necessarily arena order, especially after a partial rebuild.

## Borrowed words become owned bytes

A render method may produce text from temporary values. The frame cannot keep a borrowed string after its owner disappears. Giving the entire frame a lifetime tied to every label would spread that restriction through the runtime.

Instead, mounting copies text bytes into a frame-owned buffer. Nodes hold a small `TextRange` containing a start offset and length. That makes a text node independent of the original string's lifetime, at the price of a copy and storage in the frame's text pool. It is not a claim that UI text is free; it is an explicit place to pay for ownership.

The exact `push_text` implementation in `crates/ui/src/frame/mount.rs` is short enough to read whole:

```rust,ignore
fn push_text(&mut self, text: &str, style: TextStyle) -> Result<NodeId, MountError> {
    let start = self.text.len();

    self.text
        .extend_from_slice(text.as_bytes(), MountError::TextStorageFull)?;

    let range = TextRange {
        start,
        len: text.len(),
    };

    let node = match self.push_node(NodeKind::Text { text: range }) {
        Ok(node) => node,
        Err(error) => {
            self.text.truncate(start);
            return Err(error);
        }
    };

    self.node_mut(node).text_style = style;

    Ok(node)
}
```

`Result` makes two capacity failures visible. The text buffer may reject the bytes; if it accepts them but node insertion fails, `truncate(start)` gives those bytes back. The `?` operator propagates the first failure, while the second needs explicit cleanup. Notice the narrowness of the guarantee: this insertion rolls back its text on node failure. It does not mean every larger rebuild is transactional. The partial-rebuild implementation explicitly says an error can leave the frame inconsistent and requires clearing it.

Both offsets count bytes. Rust's `str::len()` does not count visible letters. Because the bytes come from valid strings and ranges are recorded at insertion boundaries, the frame can recover the original UTF-8 slice. Layout will worry about what those bytes look like; storage only needs to preserve them accurately.

## Keeping the parts that did not change

Mounting an entity inserts an entity node carrying its `EntityId`, a render function pointer, and an `expanded` flag. It does not immediately recurse through every nested entity. The expansion loop in `crates/ui/src/frame/rerender.rs` scans appended nodes and renders unexpanded entities. Rendering may append further entity nodes, which the same loop will eventually reach.

This is a useful distinction between describing a boundary and executing the work behind it. A stored function pointer lets the uniform frame call the correctly typed render implementation without giving every frame node a different Rust type. Iterating over pending entity nodes also avoids recursive *entity expansion*, although this does not promise that every operation involved in constructing elements is nonrecursive.

When an entity needs to render again, its old content is detached and replacement nodes are appended. Unchanged child entities can retain their content. This exact portion of the expansion loop shows the reuse decision:

```rust,ignore
if let Some(previous) = self.take_detached_entity(entity) {
    if !runtime.entities.needs_render(entity) {
        self.move_content(previous, node);
        continue;
    }

    self.detach_content(previous)?;
}

self.render_entity_node(node, runtime)?;
```

`if let` opens the optional previous location. `continue` means that retained content is already sufficient; no render call follows. The arena is compacted after the rebuild to remove detached storage. Consequently, even a frame that persists cannot promise stable node indices. `NodeId` is a working address inside the current representation, not a durable name for a button.

## Names that outlive positions

The scrolled drawer needs the durable name. InkPaper separates an element's local `ElementId` from its resolved `ElementStateId`. The identity key combines the local name with a parent identity, using these exact definitions from `crates/ui/src/element/state/mod.rs`:

```rust,ignore
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum IdentityParent {
    Entity(EntityId),
    Element(ElementStateId),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ElementStateKey {
    pub(crate) parent: IdentityParent,
    pub(crate) local: ElementId,
}
```

The resolver walks upward to the nearest identified ancestor or entity boundary. Unnamed wrappers do not add a scope. Two controls with the same local name can therefore be different controls under different identified parents; inserting an unnamed layout wrapper need not rename everything beneath it. Identifying that wrapper, however, changes the identity path. Identity is part of the UI's behavior, not decorative metadata.

The state table also records the owning entity scope and when entries were seen. When an entity renders again, state for elements it no longer produces can be swept rather than accumulating indefinitely. Scroll offsets and pressed state refer to resolved identities, allowing the frame representation to move independently.

One alternative is to retain every widget object and mutate it in place. That can make identity straightforward but ties construction, state, and traversal together. Another is to rebuild everything and restore state by array position. That is simple until insertion makes yesterday's third row today's fourth. InkPaper instead retains a working frame while assigning explicit meaning to the elements that need continuity. The application must choose those names thoughtfully, but storage addresses no longer masquerade as user-visible identity.

## Try it: move a wrapper, not a control

For an optional hardware-free trace, open `identity_parent` and `resolve_identities` in `crates/ui/src/frame/resolve.rs`. On paper, place an identified `row` beneath an entity, with one unnamed container between them. Follow the upward loop: the row's parent identity is the entity. Add another unnamed container and repeat; the result is unchanged. Now give the intermediate container an identity. The row resolves beneath that element's state instead.

The observable outcome is the selected `IdentityParent` branch, not a screenshot. You should be able to explain why moving a `NodeId` during compaction need not change this key, while changing an identified ancestor can. That distinction is what lets the drawer change its contents without forgetting where your finger left it.
