# 14. Slow work without tangled UI state

Opening a folder begins as a tiny interaction: select a row. Finishing it may require waking storage, reading directory entries, allocating names, and reporting an error if the card is unavailable. A dictionary lookup can take longer still when its index needs preparing. The user should see an understandable transition, not a listener that disappears into filesystem machinery.

InkPaper separates expressing an intention from carrying it out. Synchronous UI code records a request. An asynchronous application service removes that request, performs the operation through a platform interface, and briefly re-enters the UI to apply the answer. This keeps the ownership boundary clear. It also gives the application a place to decide which work should happen first—and when work must pause so a status frame can become visible.

## Leave a request, not a suspended listener

The browser stores pending work in an `Option<BrowseRequest>`. Its `take_request` uses `self.pending.take()`: ownership of the request moves to the caller, and the slot becomes `None`. A newer pending intention can replace an older one before it is taken. This is appropriate for “show this directory,” where the latest destination often matters more than visiting every intermediate destination. It would not be an appropriate default for an audit log or a sequence of payments.

The application's `AppRequest` enum combines frontlight, network, preferences, dictionary, cover, browser, reader, history, and file-transfer work. It does not collect all pending slots in one sweep. `InkPaperApp::take_request`, in `crates/app/src/app/request.rs`, chooses the first available request using a lazy `Option` chain. This exact tail of that chain shows part of the policy:

```rust,ignore
.or_else(|| self.lookup.take_request().map(AppRequest::Dictionary))
.or_else(|| self.cover.take_request().map(AppRequest::Cover))
.or_else(|| self.browser.take_request().map(AppRequest::Browse))
.or_else(|| self.reader.take_request().map(AppRequest::Reader))
.or_else(|| {
    self.reading_history
        .take_request()
        .map(AppRequest::ReadingHistory)
})
.or_else(|| {
    self.file_transfer
        .take_request()
        .map(AppRequest::FileTransfer)
})
```

`map` wraps a specific request in the larger enum. `or_else` runs its closure only when the preceding result is `None`. Laziness is essential because taking a request changes state. Eagerly evaluating every alternative would empty later slots even when an earlier request wins, losing the work those values represented.

The complete priority order is frontlight setting, frontlight preferences, clock sync, WiFi join, WiFi scan, saving WiFi networks, saving clock preferences, reader preferences, dictionary, cover, browser, reader, reading history, and file transfer. Reader work precedes history so progress changes reach the service before Home or Recent Books requests a snapshot. These are priorities between pending requests, not preemption: a newly urgent setting does not interrupt an operation already being awaited.

There is another ordering rule outside this chain. `AppService::service_pending` checks the desired reader font before taking an `AppRequest`, because reader work lays out text using that font. “The enum order is the entire scheduler” would therefore be an incomplete reading of the code.

## Borrow briefly, work outside the borrow

The service loop takes one request inside `runtime.update`, then matches it outside that closure. The directory handler awaits `platform.list_directory`, converts successful entries into browser entries, and applies the result in another `runtime.update`. An error instead updates the browser's error state.

This exact success-path excerpt from `crates/app/src/service/mod.rs` begins after the platform call has returned its entries:

```rust,ignore
let entries = entries
    .into_iter()
    .map(PlatformEntry::into_browse_entry)
    .collect();

let listing = BrowseListing::new(path, entries);

runtime.update(app, move |app, cx| {
    app.apply_browse_listing(listing, cx);
})?;
```

The `move` closure brings the owned listing into the short synchronous update. There is no need to retain a borrowed browser entity across the filesystem await. The service owns the work product until the application accepts it. This is a more useful rule than “never borrow anything across await”: borrowing a bus across a transfer is ordinary async Rust. The boundary to protect here is the UI entity borrow.

The main firmware task awaits `service_pending` before rendering. Consequently, this design does **not** dispatch UI input concurrently throughout a slow directory operation. Other tasks can continue while I/O is pending, including tasks collecting input, but the main loop resumes dispatch afterward. Synchronous computation inside an async operation can also occupy the executor. Separating requests from listeners prevents tangled lifetimes and creates scheduling opportunities; it does not automatically eliminate every source of visible latency.

Making every UI listener a future would expose the entity model to those longer lifetimes. A fully separate worker processing every app request could improve concurrency, but would need explicit stale-result, cancellation, and ordering rules. The current service keeps application-state mutation serialized and makes a smaller, understandable tradeoff.

## Show the waiting state before doing the wait

Dictionary indexing demonstrates a deliberate extra step. On a first lookup, the service may discover that index samples are missing. Rather than immediately doing the long sampling pass, it marks work deferred and asks the app to show that state. This is the exact branch in `crates/app/src/service/dictionary.rs`:

```rust,ignore
let Some(outcome) = self.look_up(&request, !request.deferred).await else {
    self.deferred = true;

    runtime.update(app, move |app, cx| {
        app.apply_dictionary_deferred(request, cx);
    })?;

    return Ok(());
};
```

The surrounding `service_pending` loop checks `self.deferred` and returns, allowing the frame to be rendered and presented. On the next firmware iteration, `has_deferred_work()` enables an immediately ready wake source. No extra tap is needed to restart the operation. The deferred request then permits the index sampling instead of postponing it again.

That distinction is small in code and large in experience. Setting “indexing” in application state is not enough if a long operation immediately follows and no frame can show the new state. Presentation must occur between the announcement and the work. This path provides that ordering; it is not a general progress-streaming mechanism or a guarantee that all long operations show intermediate frames.

A different early return protects a resource boundary. After file-transfer service, `service_pending` returns rather than continuing to storage-backed requests. A successful USB Drive transition makes the filesystem unavailable to normal application use. Scheduling here is about valid ownership of storage, not merely making the interface feel quick.

## A platform operation becomes a conversation

`AppPlatform`, in `crates/app/src/service/platform.rs`, describes operations rather than SD-controller details. Its associated `Error` type lets each platform retain its own errors; `RandomAccessSource: EpubSource` supplies the file abstraction that later chapters use for EPUB reading. Its contracts include bounded whole-file reads and support for at least four simultaneously open random-access files.

`load_state` returns `Result<Option<Vec<u8>>, Self::Error>`. Missing saved state is `Ok(None)`, bytes are `Ok(Some(...))`, and an operational failure is `Err(...)`. Keeping absence separate from failure lets first boot remain an ordinary condition. Some network operations explicitly start work and report completion later; the word `async` alone does not tell you whether a method awaits the whole operation or only its submission.

On the device, storage belongs to a dedicated task. A directory command contains an owned path and a reply slot. The caller waits for that particular slot while the task handles the command. These are exact excerpts from `firmware/x4-pro/src/firmware/rpc.rs`, with its separate `Debug` implementation omitted:

```rust,ignore
pub(crate) struct Reply<T>(Arc<Signal<CriticalSectionRawMutex, T>>);

impl<T: Send> Reply<T> {
    pub(crate) fn send(self, value: T) {
        self.0.signal(value);
    }
}
```

```rust,ignore
pub(crate) async fn call<C, T: Send, const N: usize>(
    commands: &Channel<CriticalSectionRawMutex, C, N>,
    command: impl FnOnce(Reply<T>) -> C,
) -> T {
    let reply = Arc::new(Signal::new());
    commands.send(command(Reply(reply.clone()))).await;
    reply.wait().await
}
```

The command channel queues requests and applies backpressure when full. Each call's signal holds its answer. Unlike the battery signal shared between updates, this signal is newly allocated for one call. The `Arc` gives caller and command shared ownership of that slot; the last owner releases its allocation. `send(self, ...)` consumes the reply handle, preventing a second send through that same handle. `FnOnce` lets a command-building closure move its captured path into the command.

This ties answers to callers even when calls overlap. It does not add a timeout or propagate caller cancellation into storage. If a caller disappears after enqueueing, the storage operation can still run and send to its now-unobserved slot. If a command never produces an answer, `call` can wait indefinitely. A useful abstraction states its limits as clearly as its convenience.

## Try it: trace an indexing handoff

For a hardware-free experiment, read `service_dictionary_request` in `crates/app/src/service/dictionary.rs`, its caller in `service/mod.rs`, and `deferred_work` in `firmware/x4-pro/src/firmware/mod.rs`. Assume a dictionary opens successfully, has no samples, and the first request is not deferred.

Write a five-step trace: missing samples discovered; request and service marked deferred; service returns; status frame is presented; deferred wake resumes work. On that second pass, `may_defer` is false and `sample_index().await` is reached. The observable result is a verified ordering in the current control flow: indexing cannot consume that second pass before the intervening presentation completes. No commands or hardware are required. This does not predict how long indexing or the panel will take; it explains why the waiting message gets a chance to exist on screen first.
