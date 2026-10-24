# Research: an encrypted, append-only export for one person's machines

Ticket `wallpapi-wa5.8`, under the map `wallpapi-wa5`. Feeds the keys-and-encryption ticket. Researched
2026-10-24 against the sources listed at the end; package versions are as PyPI showed them that day.
Settled 13 (the Evidence log never leaves your machines unencrypted) and 14 (an encrypted export through a
synced folder or private repo, append-only, safe to import twice) are taken as given.

## Answer

- **The shape matters more than the library.** Make the export a folder of small, sealed **segment files**:
  each install writes only files with its own name on them and never touches one again. Sync tools make
  conflict copies only when the *same* file changes on two machines, so if no file ever has two writers,
  conflict copies cannot arise in normal use. Git merges new files at different paths without conflict.
- **"Safe to import twice" comes from the IDs, not the encryption.** Every Judgement already carries
  install plus local sequence (Settled 9). Importing a segment means "add any I don't have yet", so reading
  a file twice, or reading a stray conflict copy, changes nothing.
- **age, through `pyrage`, is the strongest fit.** A published, versioned file format with several
  independent implementations, so the export can be opened with the plain `age` command-line tool even if
  wallpapi is gone. One 0.85 MB Windows wheel, no other dependencies. Weak spots: one maintainer, the type
  stubs ship separately, and no Windows-on-ARM wheel.
- **PyNaCl is the solid alternative**: older, maintained by the Python Cryptographic Authority, typed,
  wheels for every Windows flavour, but you design the file format yourself, and only code that follows
  it can read it.
- **Use a random key, not a typed passphrase, to encrypt each file.** age's passphrase mode spends about one
  second of deliberate work per file, encrypting and decrypting; across thousands of segments that is
  minutes. A common pattern: generate one key, keep a passphrase-protected copy of it in the synced folder
  so a new machine can unlock it once, then keep it locally (a key file or the Windows Credential Manager).
- **Integrity has two halves.** Encryption detects any change to, or cutting short of, one file. It cannot
  notice a whole file going missing; contiguous sequence numbers per install can.

## The problem in plain terms

Several installs (home, work) each append Judgements. Each needs to publish what it has recorded and read what
the others have, through a folder that Dropbox, OneDrive, Syncthing or a private git repo copies between the
machines. The folder sits on someone else's server, so everything in it must be encrypted (Settled 13). The
sync tool knows nothing of the contents: it works file by file, and when it sees one file changed in two
places it keeps both under different names.

## How sync tools behave when two machines change one file

- **Syncthing**: when "a file has been modified on two devices simultaneously and the content actually
  differs", the version with the older modification time is renamed
  `<name>.sync-conflict-<date>-<time>-<device>.<ext>`. Conflict copies "are treated as normal files after
  they are created, so they are propagated between devices".
- **Dropbox**: two edits at once, an offline edit, or a file left open on another machine produce a copy
  named with the editor, "conflicted copy" and the date. The advice is to "compare both versions and merge
  them manually".
- **OneDrive (work or school)**: for a non-Office file, "OneDrive automatically keeps both versions"; the
  local copy gets the device name appended (`Report-JOHNS-SURFACE.txt`), up to 5 conflict versions, and
  "it's up to you to decide how to treat these file versions".
- **git**: no copy; a merge where both sides changed one path stops with that path in conflict. An
  encrypted file has no meaningful line merge; git's `binary` driver keeps your side "in the conflicted
  state for the user to sort out". New files at different paths merge cleanly.

Sources: Syncthing "Understanding Synchronization"; Dropbox "conflicted copy"; Microsoft Learn "Resolve sync
issues in OneDrive for work or school"; git `gitattributes` (built-in merge drivers).

Two consequences follow, whichever library is chosen:

1. **No file may have two writers.** A shared index, a "latest" pointer or a single shared log file would
   produce conflict copies the moment two machines write between syncs. Per-install files avoid it
   entirely: install A only ever creates files named for A.
2. **Never trust a file name.** A conflict copy, a rename or a half-arrived file can appear under any name.
   The importer reads what is *inside* each file (which install wrote it, which sequence range it covers)
   and treats a file name as a hint at most. Syncthing writes incoming data to a temporary
   `~syncthing~<name>.tmp` file and renames it into place, so a reader there never sees a half-file; other
   tools' pages read here make no such promise, so the format must detect a truncated file (below).

The one way a conflict copy can still appear with per-install files: the same install ID running on two
machines, for example after copying `~/.wallpapi` to a new laptop. Then two different files can claim the
same install and sequence numbers. That is a real fork, not a sync glitch; the importer should report it
rather than pick one.

## Appending: one growing file or many sealed segments

- **Sealed segments (recommended shape).** Each export run writes one new file covering a sequence range,
  for example `<install>/<first>-<last>`, written to a temporary name and renamed into place (the pattern
  Invariant 10 already uses for **Library** writes). Nothing is ever appended in place, so a crash leaves
  either the old state or a whole new file. Both age and libsodium's stream format mark the last chunk of a
  file, so a file cannot be extended after it is finished anyway: "appending" to an encrypted stream means
  writing a new one. Cost: the file count grows; Settled 8 already allows compaction in exported snapshots,
  which bounds it. In git, deleting old segments after a snapshot does not shrink history.
- **One growing file per install, records appended in place.** Possible only with independently encrypted
  records (for example one PyNaCl `SecretBox` message per record, each length-prefixed), since an age file
  is sealed once finished. Fewer files, but every append is a non-atomic write: a crash or a half-synced
  copy leaves a torn last record that the reader must recognise and skip. A sync tool that sends whole
  files re-sends all of it on every append.
- **One file, rewritten whole each time.** Simple and atomic, but the cost grows with the log; at "unbounded
  and cheap" (Settled 17) it does not scale.

Encrypted bytes do not compress, so any compression has to happen before encryption.

## The options compared

**age via `pyrage`**

- *What*: a Python binding (PyO3) to the Rust `age` crate 0.12.1, which implements the age v1 format.
- *Readable without wallpapi*: yes, by the Go `age` tool, `rage` or typage.
- *Releases*: 1.4.0 on 2026-08-23; first release 2022-07-20; 21 releases. MIT.
- *Maintainers*: one person (GitHub `woodruffw`), 12 contributors, 92 GitHub stars.
- *Windows*: one abi3 `win_amd64` wheel, 0.85 MB, works on any CPython from 3.10, so 3.14 too. No
  `win_arm64` and no 32-bit wheel: on Windows on ARM it would build from source with a Rust toolchain.
- *Dependencies*: none at runtime.
- *Types*: stubs ship separately as `pyrage-stubs` (same version line), needed for pyright strict.
- *Integrity within a file*: header MAC, then a tag on every 64 KiB chunk; a cut-short file fails.
- *Large files*: `encrypt_file`/`encrypt_io` stream in recipient-key mode; passphrase mode is bytes in,
  bytes out.
- *Passphrase*: built in (scrypt), about one second per file, no setting exposed by pyrage.
- *Who can create a valid file*: anyone with the *public* recipient key (key mode); passphrase holders.

**PyNaCl `SecretBox` / `Aead`**

- *What*: a Python binding (cffi) to libsodium's one-message encryption. No file format: you design it.
- *Readable without wallpapi*: no, unless someone follows your written format.
- *Releases*: 1.6.2 on 2026-01-01; first release 2013-04-14. Apache-2.0.
- *Maintainers*: the Python Cryptographic Authority, the group behind `cryptography`.
- *Windows*: abi3 wheels for `win_amd64`, `win_arm64` and `win32`, about 0.24 MB, libsodium bundled.
- *Dependencies*: `cffi` (0.19 MB wheel, has 3.14 Windows wheels) and the pure-Python `pycparser`.
- *Types*: ships `py.typed`.
- *Integrity within a file*: one 16-byte tag over the whole message; `Aead` can also authenticate
  unencrypted "additional data".
- *Large files*: the whole message is in memory, which is fine for segment-sized files.
- *Passphrase*: not built in; derive a key with `nacl.pwhash` (Argon2id by default).
- *Who can create a valid file*: only key holders.

**PyNaCl secretstream**: same package, low-level bindings only. Chunked like age, and libsodium promises
messages "cannot be truncated, removed, reordered, duplicated or modified without this being detected".
Worth it only if segments grow large enough to stream.

**`cryptography` Fernet**: AES-128-CBC plus HMAC-SHA256, one token per message, the token carrying its
creation time in plain text. 50.0.2 on 2026-09-30, about a 3.8 MB Windows wheel plus `cffi`. The docs call
it "generally unsuitable for very large files". No advantage over PyNaCl here and a heavier install.

None of these is already in `uv.lock`, so each adds what its "Dependencies" line says. A pure-Python
package named `age` exists on PyPI (0.5.1) but its last release was 2023-05-06 and it pulls in PyNaCl,
`cryptography`, `bcrypt`, `click` and `requests`; it is not a contender.

### age in more detail

- **Format maturity.** The Go reference tool reached v1.0.0 on 2021-09-06 (latest v1.3.2, 2026-08-29). The
  format spec is versioned (`age-encryption.org/v1`) and has interoperable implementations: rage in Rust
  and typage in TypeScript. That is the main argument for age here: the export outlives the app, and
  recovery needs only the key and a stock tool.
- **How a file works.** A short text header holds a random per-file key, wrapped for each recipient,
  then an HMAC over the header. The body is split into 64 KiB chunks, each sealed with ChaCha20-Poly1305;
  the last chunk carries a "final" flag, and the spec requires that "streaming decryption MUST signal an
  error if the end of file is reached without successfully decrypting a final chunk". A half-synced file
  therefore fails cleanly and can be retried later.
- **Two ways to unlock.** *Recipient keys* (X25519): a public key encrypts, the matching identity (a short
  `AGE-SECRET-KEY-1…` string) decrypts. *Passphrase* (scrypt): rage picks a work factor that "will take
  around 1 second on this device", and decryption pays about the same. pyrage's passphrase functions take
  and return whole byte strings and expose no work factor; only the recipient-key path has file and stream
  variants.
- **No sender authentication in key mode.** The spec forbids mixing a passphrase stanza with any other "to
  uphold an expectation of authentication that is implicit in password-based encryption"; the corollary is
  that in recipient-key mode anyone who knows the public key can write a file the identity will decrypt.
  For a private folder that is a small risk, but it means "decrypts fine" does not prove "one of my
  installs wrote this". The public key does not need to be published anywhere, which keeps that risk small.
- **One identity for all installs.** Every install must decrypt every other install's files, so in practice
  all installs share one identity. Encrypting to a list of per-install keys is possible, but adding a
  machine would then mean re-encrypting old segments to it.

### PyNaCl in more detail

- `SecretBox` (XSalsa20-Poly1305) and `Aead` (XChaCha20-Poly1305) take a 32-byte key and a 24-byte nonce,
  generated randomly by default; output is 40 bytes longer than the input. The nonce "must NEVER be reused
  for a particular key"; with random 24-byte nonces that is not a practical concern.
- `Aead` accepts "additional data" that is authenticated but not encrypted: a natural place to bind the
  install ID and sequence range so a renamed or swapped file is caught.
- secretstream exists in `nacl.bindings` (`crypto_secretstream_xchacha20poly1305_*`) but has no high-level
  class; libsodium says its messages "cannot be truncated, removed, reordered, duplicated or modified
  without this being detected".
- For a passphrase, `nacl.pwhash` derives a key with Argon2id; the salt and cost settings must be stored
  alongside by the caller: the docs say the library does not record them for raw key derivation.
- 1.6.2 updated the bundled libsodium to fix CVE-2025-69277; the wheels bundle libsodium, so nothing else
  is installed on Windows.

## Keys: passphrase, key file or OS keyring

If the export is written in the background (like today's refill threads), whatever unlocks it must be
available without a person at the keyboard. That pushes every option towards a key stored on each machine.

Two separate questions: what encrypts each file, and where each machine keeps the secret.

**What encrypts each file**

- **A passphrase, on every segment** (age passphrase mode). Nothing extra to manage, and the passphrase
  alone recovers everything. But it costs about a second per file in each direction, background export
  still needs the passphrase stored somewhere, and a weak passphrase can be guessed offline against any
  segment in the cloud folder.
- **A random key** (an age identity, or 32 random bytes for PyNaCl). Instant, and no guessing attack on the
  files. The key has to reach each machine, and losing every copy loses the log.
- **A random key, wrapped by a passphrase** (the hybrid). The files use the random key; the synced folder
  also holds one passphrase-protected copy of the key (an age passphrase file, or a PyNaCl box under an
  Argon2id-derived key). A new machine asks for the passphrase once, unwraps the key and keeps it locally.
  Recovery needs only the passphrase and the folder. A weak passphrase is still guessable offline, but only
  against that one small file, at a deliberately slow cost per guess.

**Where each machine keeps the key**

- **A key file** in `~/.wallpapi/`, outside the synced folder. No dependencies; readable by any program
  running as you; included in whatever backs up that directory.
- **The OS keyring** through the `keyring` package, which on Windows uses Credential Manager. Adds `keyring`,
  `pywin32-ctypes` and the `jaraco.*` helpers (all pure Python). Windows caps a stored secret at 2,560 bytes
  (`CRED_MAX_CREDENTIAL_BLOB_SIZE`), ample for a 32-byte key or an age identity. `keyring` stores with
  `CRED_PERSIST_ENTERPRISE`, which Windows makes visible "to logon sessions for this user on other
  computers" when the account has a roaming profile and keeps local otherwise. The `keyring` docs say of
  Windows Credential Locker that "No analysis has been performed", so it is not documented as stronger than
  a file in your profile; the practical difference is that the key is not a file that a backup or a copy of
  `~/.wallpapi` sweeps up.

## Integrity, end to end

- **Inside one file**: all four options detect any change with the key. age and secretstream also detect a
  file cut short; a single `SecretBox` message fails as a whole, which is the same outcome.
- **Which file is which**: put the install ID and sequence range inside the encrypted content (or in
  `Aead`'s additional data), so the importer never relies on the file name.
- **A missing file**: no encryption scheme notices a file that never arrived. Contiguous per-install sequence
  ranges do: install A at 1-40, 41-90, 120-150 has a visible gap.
- **An old file shown again**: harmless, because importing is idempotent by (install, sequence).

## Options for the keys-and-encryption ticket

The decision belongs to that ticket; these are the choices this research leaves on the table.

1. **age recipient key, wrapped by a passphrase** (age for the segments; an age passphrase file holding the
   identity in the synced folder; the identity cached locally). Open format, stock-tool recovery,
   passphrase cost paid once per machine. Costs: a single-maintainer binding, no ARM wheel, key-mode files
   can be forged by whoever has the public key.
2. **age passphrase on every segment.** Simplest to explain and nothing to store, but the passphrase must
   still be stored for background export, and about a second per segment each way makes large imports slow
   unless snapshots keep the file count low.
3. **PyNaCl `Aead` with a random key, wrapped by an Argon2id passphrase.** Best-maintained dependency, typed,
   every Windows wheel, authenticated metadata; but a home-made format that only wallpapi can read, so its
   specification becomes something to write down and keep.

Independent of the choice: per-install sealed segments, written atomically, identified by their contents,
imported by (install, sequence), with gaps and forks reported rather than resolved silently.

## Sources

- age format spec (C2SP): <https://github.com/C2SP/C2SP/blob/main/age.md> (stable URL c2sp.org/age)
- age README and releases: <https://github.com/FiloSottile/age>,
  <https://api.github.com/repos/FiloSottile/age/releases>
- rage README and scrypt work factor: <https://github.com/str4d/rage>,
  <https://github.com/str4d/rage/blob/main/age/src/native/scrypt.rs>
- pyrage README, source and Cargo.toml: <https://github.com/woodruffw/pyrage>,
  <https://github.com/woodruffw/pyrage/blob/main/src/lib.rs>,
  <https://github.com/woodruffw/pyrage/blob/main/src/passphrase.rs>
- PyPI JSON metadata (versions, wheels, sizes, dependencies) for `pyrage`, `pyrage-stubs`, `PyNaCl`,
  `cffi`, `cryptography`, `keyring`, `age`: <https://pypi.org/pypi/{name}/json>
- PyNaCl docs: <https://pynacl.readthedocs.io/en/latest/secret/>,
  <https://pynacl.readthedocs.io/en/latest/password_hashing/>; secretstream bindings:
  <https://github.com/pyca/pynacl/blob/main/src/nacl/bindings/crypto_secretstream.py>
- libsodium secretstream: <https://doc.libsodium.org/secret-key_cryptography/secretstream>
- cryptography Fernet: <https://cryptography.io/en/latest/fernet/>
- keyring docs and Windows backend: <https://keyring.readthedocs.io/en/latest/>,
  <https://github.com/jaraco/keyring/blob/main/keyring/backends/Windows.py>
- Windows `CREDENTIALW`: <https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw>
- Syncthing: <https://docs.syncthing.net/users/syncing.html>
- Dropbox: <https://help.dropbox.com/organize/conflicted-copy>
- OneDrive: <https://learn.microsoft.com/en-us/troubleshoot/sharepoint/sync/troubleshoot-sync-issues>
- git merge drivers: <https://git-scm.com/docs/gitattributes>
