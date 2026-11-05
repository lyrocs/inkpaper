# 9. Changing the screen without disturbing the page

A button darkens under your finger while the book page behind it stays still. That small response should not require rebuilding every paragraph or treating the entire display as new content. On an e-paper reader, unnecessary work can also become an unnecessary physical update, with its attendant delay and visible disturbance.

There are several opportunities to avoid that work, and they are easy to confuse. Rebuilding produces frame content from application state. Layout assigns geometry. Painting changes pixels in a destination buffer. Refreshing asks the display hardware to transition to those pixels. Doing less at one stage does not automatically mean doing less at every other stage.

InkPaper communicates across these stages with invalidation, damage, and a separate refresh policy. Their common rule is conservative: when precise bookkeeping runs out, do more work rather than leave stale content visible. The excerpts below are current implementation fragments, not independent programs.

## What became stale?

A pressed style that changes only a color does not need new geometry. Changing geometry requires layout. Changing the elements returned by an entity requires rendering and mounting them again. `Invalidation` names the work categories: `None`, `Paint`, `Layout`, and `Rebuild`.

Multiple changes can arrive before the platform prepares another frame. Their requirements must accumulate, not overwrite one another. This exact method from `crates/ui/src/rendering/invalidation/mod.rs` combines them:

```rust,ignore
pub const fn merge(self, other: Self) -> Self {
    use Invalidation::*;

    match (self, other) {
        (Rebuild, _) | (_, Rebuild) => Rebuild,
        (Layout, _) | (_, Layout) => Layout,
        (Paint, _) | (_, Paint) => Paint,
        _ => None,
    }
}
```

Each arm states the dominating requirement. The `|` joins alternative patterns, and `_` ignores the other value. This is not an instruction to find the cheapest change in the batch; it finds the strongest requirement needed to satisfy all of them. A repaint request cannot cancel a pending rebuild.

`Runtime::prepare_frame` consumes this requirement. Paint-only work keeps the frame and layout. Layout runs the layout pass. Rebuild performs rebuilding followed by layout, then determines damage. This separation also explains why ordinary state mutation still needs notification: changing a field does not, by itself, tell the runtime which descriptions have become stale.

The category is a work requirement, not a promise about pixel area. Layout invalidation is conservatively full, while a rebuild can ultimately produce narrow damage if the runtime can prove that the change remained local. “Rebuild” therefore does not mean “flash the whole panel.”

## Where can old pixels survive?

Damage answers a different question: which areas need their pixels repainted? A moved or shortened label requires attention to the old area as well as its new bounds. Otherwise the new text looks right while yesterday's trailing letters remain beside it.

`DamageRegion` keeps up to four rectangles and a separate full flag. Touching or overlapping rectangles merge. When a merged candidate grows, the scan restarts because it may now meet a rectangle already examined. If the bounded list still cannot hold another disjoint rectangle, this exact tail of `add_rect` collapses it:

```rust,ignore
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
```

The unusual `let Err(candidate) ... else` handles success first: if insertion succeeds, the function returns. On failure the collection returns ownership of the rectangle, so it becomes the starting value for `fold`. Each stored rectangle is unioned into that value. `copied()` converts borrowed rectangles into small by-value inputs.

One large rectangle may include many unchanged pixels. That costs drawing work, but it still includes every damaged pixel. Discarding the fifth rectangle would be cheaper and wrong. An unbounded region structure could preserve more precision, but would make memory consumption depend on the shape and number of changes. This fixed limit makes the fallback predictable.

Full damage is not represented by a screen-sized rectangle. In fact a full region has no stored rectangles, so testing `rects().is_empty()` is not a valid “nothing changed” test. Use `is_none()`. The semantic flag must survive transformations even when a full rectangle would look geometrically equivalent.

## Saving render work as well as paint work

Partial painting alone would still waste time if every small change called every entity's render method. InkPaper tracks dirty entities and retains unaffected subtrees, as chapter 6 described. It also records dependencies: an entity whose render reads another entity or a global needs to be reconsidered when that source changes.

The exact notification entry point in `crates/ui/src/runtime/cx.rs` is deliberately small:

```rust,ignore
/// marks `entity` and the entities that read it as needing to render again
pub(crate) fn notify(self, entity: EntityId) {
    self.entities.mark_dirty(entity);
    self.mark_readers(Source::Entity(entity));
}
```

The dependency table contains reader/source pairs. Rendering forgets an entity's previous reads and records the current ones, so conditional reads matter. A hidden view need not depend on state it does not inspect. This is more precise than manually declaring that every part of the application depends on every status value.

Precision still has a limit. If a dependency cannot be stored, the table marks itself incomplete and a subsequent change dirties everything. Losing an edge silently would make a view stop updating. The conservative fallback is the same design decision as the damage list, applied to a different stage.

After a partial rebuild, damage includes what the replaced subtrees painted before and what they paint now. If layout also moves or scrolls nodes outside those subtrees, or their old painted area is unknown, the runtime cannot safely claim that only those areas changed. It abandons the narrow result. A small entity is an opportunity for local updates, not a guarantee that its geometry cannot affect neighbors.

Painting can then avoid whole branches using cached subtree paint bounds. Looking only at the parent's own rectangle would be unsafe because children can overflow it. Ordered sibling pruning can skip distant runs in regular flows, but the implementation disables that optimization when positioning can break geometric order. A clever shortcut is useful only while its assumptions remain true.

## The panel has the last word

Logical damage may have edges the hardware cannot address. `RefreshRegionConstraints` clips rectangles to the display, aligns them outward, and clips again. Alignment can cause formerly separate rectangles to merge. Outward rounding is important: inward rounding could omit a changed edge pixel.

The following exact early branches of `normalize`, in `crates/ui/src/rendering/presentation/mod.rs`, preserve the two nongeometric cases; its ordinary rectangle path follows afterward:

```rust,ignore
if damage.is_none() {
    return DamageRegion::none();
}

// full damage is semantic, not merely geometric.
// do not convert it into a rectangle coverting `bounds` because that would turn
// a requested full refresh into a partial refresh covering the whole screen
if damage.is_full() {
    return DamageRegion::full();
}
```

Preserving this full marker does not mean the firmware blindly equates it with a full waveform. `firmware/x4-pro/src/firmware/refresh_policy.rs` considers the controller's established state and update capabilities too. Eligible binary differential updates and eligible grayscale-window updates can choose `Fast` even for large or full damage. A full repaint and a full physical transition remain distinct.

The policy gives some safeguards precedence over that fast path: an unknown zero display size, grayscale content without window eligibility, and the periodic cleanup limit select `Full`. The exact threshold constants are:

```rust,ignore
const MAX_CONSECUTIVE_FAST_REFRESHES: u8 = 12;
const LARGE_DAMAGE_PERCENT: u8 = 60;
const CONTINUOUS_TONE_DAMAGE_PERCENT: u8 = 20;
```

The area thresholds apply later in the decision chain, after eligibility checks. Quoting “60 percent means full refresh” without that context would misdescribe the code. Likewise, “grayscale always needs a full refresh” is too broad: a valid grayscale baseline and a capable controller can support the window path.

These are device-policy choices, not universal facts about every e-paper panel. The simulator can show whether the right pixels were painted, but it cannot establish physical ghosting, waveform timing, or energy cost. Those are reasons to keep the hardware decision explicit rather than pretending that a small rectangle explains the whole update.

## Try it: exceed the precision budget

For an optional read-only experiment, open `damage_region_collapses_when_capacity_is_exceeded` in `crates/ui/src/rendering/invalidation/tests.rs`. Follow the first four disjoint rectangles: each can occupy a slot. The fifth forces one conservative bounding rectangle. Check that every original rectangle lies inside it and that `is_full()` is still false; collapsing precision is not the same operation as requesting full damage.

Then trace `RefreshPolicy::select` with binary differential eligibility enabled and full damage set. With fewer than twelve consecutive fast updates and a valid display size, it reaches `Fast`; at the cleanup limit it reaches `Full` first. The observable result is the selected branch. Together, these traces separate memory limits, repaint area, and physical refresh policy without requiring a reader on your desk.
