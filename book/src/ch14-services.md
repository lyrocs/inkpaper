# 14. Services: platforms, requests and replies

> **Where we are:** `1a9a532` "add hardware-independent application shell"
> (19 August 2026), the storage work of late August, the September split into an
> app service (`182d513` "extract x4 app service", `ce776c6`/`c1c7a78` "move
> simulator/x4 orchestration into app", `d6cb386` "enforce app service
> boundary"), persistence formats (`e977ecf` postcard, `58192c4` minicbor), and two
> refinements: `bd97634` (per-command reply slots, 11 October) and `c2e1e53`
> (one app request at a time, 5 November).

## The problem: UI code can't wait

The UI runtime is synchronous. `render`, listeners and `update` are plain
functions that must finish quickly. But a lot of what an e-reader does is slow
and asynchronous: list a folder on the SD card, open an EPUB, read a chapter,
save the reading position, scan for WiFi networks.

Two bad solutions come to mind:

- Make the UI async. Every listener would become a future, and borrowing
  entity state across `.await` points would be a nightmare.
- Let listeners block on I/O. The screen would freeze, and on the device they
  couldn't block anyway, because the storage task runs on the same executor.

InkPaper uses a third design: **the UI records what it wants, and a separate
async service does it later.**

```text
 tap ──► listener ──► app state: "please list /Books"   (sync, instant)
                                    │
           main loop ──► AppService::service_pending()   (async)
                                    │  take_request()
                                    ▼
                          platform.list_directory("/Books").await
                                    │
                                    ▼
             runtime.update(app, |app, cx| app.apply_listing(...))
```

## Requests as data

When the user opens a folder, the browser state just stores the wish:

```rust
pub(crate) fn take_request(&mut self) -> Option<BrowseRequest> {
    self.pending.take()
}
```

`pending` is an `Option<BrowseRequest>`, a **slot**. A newer request simply
replaces an older one (tapping two folders quickly lists only the second). Each
part of the app has its own slot, and the app collects them into one enum:

```rust
/// work the app asks the service for
#[derive(Debug)]
pub(crate) enum AppRequest {
    Frontlight(FrontlightSetting),
    FrontlightPreferences(FrontlightPreferencesRequest),
    /// sync the clock over the network this plan joins
    ClockSync(WifiJoinPlan),
    WifiJoin(WifiCredentials),
    WifiScan,
    SaveWifiNetworks(SavedNetworks),
    SaveClockPreferences(ClockPreferences),
    ReaderPreferences(ReaderPreferencesRequest),
    Dictionary(DictionaryRequest),
    /// load the cover of the book at this path
    Cover(String),
    Browse(BrowseRequest),
    Reader(ReaderRequest),
    ReadingHistory(ReadingHistoryRequest),
    FileTransfer(FileTransferRequest),
}
```

The latest commit in the repository (`c2e1e53`) changed how the service collects
them. Instead of polling fourteen slots one by one, it asks the app for **the next
request**, and the app decides the priority in one expression:

```rust
pub(crate) fn take_request(&mut self, cx: &mut Context<'_, Self>) -> Option<AppRequest> {
    self.frontlight
        .update(cx, |frontlight, _| {
            frontlight
                .take_request()
                .map(AppRequest::Frontlight)
                .or_else(|| {
                    frontlight
                        .take_preferences_request()
                        .map(AppRequest::FrontlightPreferences)
                })
        })
        .or_else(|| self.take_clock_sync_request().map(AppRequest::ClockSync))
        .or_else(|| self.wifi.take_join_request().map(AppRequest::WifiJoin))
        .or_else(|| {
            self.wifi
                .take_scan_request()
                .then_some(AppRequest::WifiScan)
        })
        // ...
        .or_else(|| self.browser.take_request().map(AppRequest::Browse))
        .or_else(|| self.reader.take_request().map(AppRequest::Reader))
        .or_else(|| {
            self.reading_history
                .take_request()
                .map(AppRequest::ReadingHistory)
        })
        // ...
}
```

This is **`Option` chaining** at its best.

- `.map(AppRequest::Browse)` wraps a `BrowseRequest` into the enum, using the
  variant as a function again.
- `.or_else(|| ...)` tries the next source *only if* the previous one returned
  `None`. It takes a **closure** so the later sources aren't touched at all once
  one returns `Some`. That matters, because `take_request` *removes* the request
  from its slot. With `.or(...)` instead, every slot would be emptied on every
  call and all but the first request would be lost.
- `bool::then_some(value)` turns `true` into `Some(value)` and `false` into
  `None`, which suits the WiFi scan, a request with no data.

The order of the chain *is* the priority policy, and the doc comment says why:
"Reader requests come before history, so a page turn's progress is saved before
Home and Recent Books take their snapshot."

## The service

`AppService<P>` takes requests one at a time and performs them:

```rust
loop {
    let Some(request) = runtime.update(app, |app, cx| app.take_request(cx))? else {
        return Ok(());
    };

    match request {
        AppRequest::Frontlight(setting) => {
            let _ = self.platform.set_frontlight(setting).await;
        }
        AppRequest::WifiScan => {
            if self.platform.start_wifi_scan().await.is_err() {
                runtime.update(app, |app, cx| {
                    app.apply_wifi_scan_result(Err(WifiScanError), cx);
                })?;
            }
        }
        AppRequest::Browse(request) => {
            self.service_browse_request(runtime, app, request).await?;
        }
        AppRequest::Reader(request) => {
            self.service_reader_request(runtime, app, request).await?;
        }
        AppRequest::FileTransfer(request) => {
            self.service_file_transfer_request(runtime, app, request)
                .await?;

            // A successful USB Drive transition makes the filesystem unavailable.
            // Do not loop around and try to service another storage-backed request.
            return Ok(());
        }
        // ...
    }
}
```

The pattern is: borrow the app briefly to take a request (sync), release it,
`.await` the platform (async), then borrow the app briefly again to apply the
result (sync). **No entity borrow is ever held across an `.await`.** That's the
rule that makes sync UI and async I/O coexist. If a borrow were held across an
await, any input event processed while waiting would hit a `BorrowConflict`.

The `match` is **exhaustive**. Adding a new `AppRequest` variant breaks the build
until the service handles it, so a request can't be silently ignored.

## The platform trait

What does "list a directory" mean? On the device, it's a FAT32 filesystem on an
SD card, accessed by another task. In the simulator, it's a folder on your
laptop. The app abstracts that behind a trait:

```rust
#[allow(async_fn_in_trait)]
pub trait AppPlatform {
    type Error;
    type RandomAccessSource: EpubSource;

    async fn list_directory(&mut self, path: &str) -> Result<Vec<PlatformEntry>, Self::Error>;

    /// Opens `path` to read anywhere in it. At least four files stay open
    /// together, a book and a dictionary's; opening more may close the one
    /// read least recently.
    async fn open_random_access(
        &mut self,
        path: &str,
    ) -> Result<Self::RandomAccessSource, Self::Error>;

    /// Reads the whole file at `path`, refusing one over `max_bytes`.
    async fn read_file(&mut self, path: &str, max_bytes: usize) -> Result<Vec<u8>, Self::Error>;

    async fn load_state(
        &mut self,
        name: &str,
        max_bytes: usize,
    ) -> Result<Option<Vec<u8>>, Self::Error>;

    async fn save_state(&mut self, name: &str, bytes: &[u8]) -> Result<(), Self::Error>;

    /// Bytes unique to this device, used to obfuscate saved WiFi passwords.
    fn device_key(&self) -> DeviceKey;

    /// Starts a WiFi scan and returns without waiting. The platform reports the
    /// networks through [`InkPaperApp::apply_wifi_scan_result`].
    async fn start_wifi_scan(&mut self) -> Result<(), Self::Error>;
    // ...
}
```

Two **associated types**:

- `type Error`: the simulator uses `std::io::Error`, the device uses its own
  `StorageError`.
- `type RandomAccessSource: EpubSource`: an opened file that the EPUB parser
  (chapter 15) can read at arbitrary offsets. The bound says "whatever type you
  return, the EPUB parser must be able to read it". That connects the platform
  to the parser without either knowing the other's concrete types.

The documentation carries **contracts** that the type system can't express:
"at least four files stay open together", "refusing one over `max_bytes`",
"returns without waiting". Every implementation must honour them, and the doc
comment is where they're written down.

Look at `load_state`'s return type: `Result<Option<Vec<u8>>, Self::Error>`. The
three layers mean three different outcomes: an error (the card failed), `Ok(None)`
(nothing saved yet, a normal first boot) and `Ok(Some(bytes))`. Collapsing
"missing" into an error would force callers to inspect error kinds to tell a
first boot from a broken card.

The simulator implementation is short, ordinary `std` code:

```rust
impl AppPlatform for SimulatorPlatform {
    type Error = io::Error;
    type RandomAccessSource = HostFileSource;

    async fn read_file(&mut self, path: &str, max_bytes: usize) -> Result<Vec<u8>, Self::Error> {
        let host_path = card_folder_path(path).ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotFound, "simulated file does not exist")
        })?;

        if fs::metadata(&host_path)?.len() > max_bytes as u64 {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "file is too large"));
        }

        fs::read(host_path)
    }
    // ...
}
```

These `async fn`s never actually wait, and the simulator drives them with
`futures_lite::future::block_on`. An `async fn` doesn't *have* to await anything,
which is what makes one trait serve both a blocking desktop and an async
microcontroller.

## On the device: request/reply over channels

On the X4, the filesystem lives in the **storage task** (it also hands the SD card
to USB mass storage when you plug in a cable). The main task can't touch it
directly, so it sends commands. Each command carries its own **reply slot**:

```rust
#[derive(Debug)]
enum Command {
    ListDirectory {
        path: String,
        reply: Reply<Result<Vec<StorageEntry>, StorageError>>,
    },
    ReadFile {
        path: String,
        max_bytes: usize,
        reply: Reply<Result<Vec<u8>, StorageError>>,
    },
    SaveState {
        name: String,
        bytes: Vec<u8>,
        reply: Reply<Result<(), StorageError>>,
    },
    EnterUsbDrive(Reply<bool>),
    Shutdown(Reply<()>),
    // ...
}
```

The `Reply<T>` type and the generic `call` helper fit in 30 lines
(`firmware/x4-pro/src/firmware/rpc.rs`):

```rust
/// The one-shot reply slot a command carries back to its caller.
pub(crate) struct Reply<T>(Arc<Signal<CriticalSectionRawMutex, T>>);

impl<T: Send> Reply<T> {
    pub(crate) fn send(self, value: T) {
        self.0.signal(value);
    }
}

/// Sends the command `command` builds around a fresh reply and waits for the answer.
pub(crate) async fn call<C, T: Send, const N: usize>(
    commands: &Channel<CriticalSectionRawMutex, C, N>,
    command: impl FnOnce(Reply<T>) -> C,
) -> T {
    let reply = Arc::new(Signal::new());
    commands.send(command(Reply(reply.clone()))).await;
    reply.wait().await
}
```

This is a tiny **RPC** (remote procedure call) system.

- `Arc<Signal<...>>`: an atomically reference-counted, heap-allocated signal,
  shared by the caller (who waits on it) and the command (which carries it to the
  storage task). When both sides are done, the last `Arc` frees it.
- `Reply::send(self, ...)` takes `self` **by value**. A reply can be sent only
  once, because sending consumes it. The type system enforces the "one-shot"
  in the doc comment.
- `command: impl FnOnce(Reply<T>) -> C` lets the caller build the command
  *around* the reply. And because enum variants are functions, the simplest calls
  read almost like magic:

```rust
pub async fn shutdown_and_wait() {
    rpc::call(&COMMANDS, Command::Shutdown).await;
}

pub async fn enter_usb_drive_and_wait() -> bool {
    rpc::call(&COMMANDS, Command::EnterUsbDrive).await
}
```

`Command::Shutdown` has type `fn(Reply<()>) -> Command`, which is exactly an
`FnOnce(Reply<T>) -> C` with `T = ()`. **Type inference** works out `T` from the
variant, and the return type of `call` follows. Richer commands use a closure:

```rust
pub async fn list_directory_and_wait(path: &str) -> Result<Vec<StorageEntry>, StorageError> {
    rpc::call(&COMMANDS, |reply| Command::ListDirectory {
        path: String::from(path),
        reply,
    })
    .await
}
```

`reply,` in the struct literal is **field init shorthand** for `reply: reply`.

Before `bd97634` the firmware used one shared signal per *kind* of reply, which
was fragile if two callers overlapped. Putting the reply *inside* the command
ties each answer to its question.

### The other end

The storage task is a loop over commands:

```rust
loop {
    match COMMANDS.receive().await {
        Command::ListDirectory { path, reply } => {
            reply.send(list_directory(filesystem, &path).await);
        }

        Command::ReadRandomAccess { handle, offset, len, reply } => {
            let index = open_random_access
                .iter()
                .position(|file| file.handle().id() == handle);

            let result = match index {
                Some(index) => {
                    let result = open_random_access[index].read_exact_at(offset, len).await;

                    // most recently read last
                    open_random_access[index..].rotate_left(1);

                    result
                }
                None => Err(StorageError::StaleHandle),
            };

            reply.send(result);
        }
        // ...
    }
}
```

Destructuring the command in the `match` arm moves `path` and `reply` out of it.
The open-file list is a tiny **LRU cache** (least recently used): `rotate_left(1)`
on the tail of a slice moves the file just read to the end, and when the list is
full, `remove(0)` closes the least recently used file. It's four files, so a
`Vec` and a linear scan beat any clever data structure.

The firmware's `X4Platform` implements `AppPlatform` by calling these functions,
and the app never learns there's another task involved.

## Saving state: formats that survive upgrades

Preferences and reading progress are saved as small files. The first format was
`postcard` (`e977ecf`), a compact serde-based binary format. It encodes fields
**by position**, so adding a field changes the layout, and old files can't be
read anymore. Commit `58192c4` switched to **minicbor with indexed fields**:

```rust
#[derive(Debug, Encode, Decode)]
struct StoredReaderPreferences {
    #[n(0)]
    font_size: u16,
    #[n(1)]
    line_spacing: u8,
    #[n(2)]
    margin: u8,
    // ...
    /// on when missing, from before the setting existed
    #[n(7)]
    hyphenation: Option<bool>,
    /// the card family's name; the built-in font when missing
    #[n(8)]
    font: Option<String>,
    /// normal when missing, from before the setting existed
    #[n(10)]
    darkness: Option<u8>,
    /// off when missing, from before the setting existed
    #[n(11)]
    antialiasing: Option<bool>,
}
```

Each field has a **stable number** (`#[n(7)]`), like Protocol Buffers. New fields
get new numbers and are `Option`, so a file written by an older firmware (which
lacks field 11) still decodes, with `None` meaning "use the default". The
comments record what each `None` means. **Derive macros** (`Encode`, `Decode`)
generate all the serialisation code from these annotations.

Loading stays forgiving:

```rust
pub(super) async fn load_history(&mut self) -> ReadingHistory {
    self.read_state(READING_HISTORY_STATE, MAX_READING_HISTORY_BYTES)
        .await
        .and_then(|bytes| ReadingHistory::decode(&bytes).ok())
        .unwrap_or_default()
}
```

`.ok()` turns a `Result` into an `Option`, discarding the error. Then
`.and_then(...)` chains a second fallible step, and `.unwrap_or_default()` falls
back to `ReadingHistory::default()`. A corrupted history file must never stop the
reader from booting, and this one expression encodes that policy.

Every read has a size cap (`MAX_READING_HISTORY_BYTES = 64 * 1024`), so a
corrupted or hostile file can't make the device allocate unbounded memory.

## Rust concepts in this chapter

- **Separating sync UI from async I/O** with request slots and a service loop.
- **Never holding a borrow across `.await`.**
- **`Option` chaining**: `map`, `or_else` (lazy) vs `or` (eager), `then_some`,
  `ok`, `and_then`, `unwrap_or_default`.
- **Exhaustive `match`** as a completeness check.
- **Traits with associated types bounded by other traits**
  (`RandomAccessSource: EpubSource`), and documented contracts.
- **`Result<Option<T>, E>`** for three distinct outcomes.
- **`Arc`**, one-shot types via `self`-consuming methods, RPC over channels.
- **Enum variants as `FnOnce`**, type inference through generic helpers, field
  init shorthand.
- **`slice::rotate_left`**, tiny LRU caches.
- **Derive macros for serialisation**; stable field numbers for forward
  compatibility.

## Try it yourself

1. Swap one `.or_else(|| ...)` in `take_request` for `.or(...)` and write a test
   that queues two requests from different slots. What goes wrong?
2. Write a `MemoryPlatform` for tests that stores `save_state` calls in a
   `HashMap` and returns them from `load_state`. Which associated types does it
   need?
3. Encode a `StoredReaderPreferences` with minicbor, then decode it into a copy of
   the struct that lacks field `#[n(11)]`. Then try the opposite direction.
