# Snapshot transfer acceptance

Public Beta v0.1 stays no-go. No host is purchased. This command copies one signed snapshot upload into a private directory. It does not connect to PostgreSQL, and it does not restore, promote, roll back, or clean a database generation. `openfactors_dev` is not opened.

The publisher signing key stays on the development computer. The serving host, when one exists later, keeps only the public allowed-signers file.

## Two logins

The upload login and the acceptance login are different. `of_api` is neither of them.

| Login | How it reaches the machine | What it may do |
|---|---|---|
| `of_publish_transfer` | SFTP only. No shell, no TTY, no forwarding. | Write the four `.partial` names under its chroot. |
| `of_publish_accept` | A local command. No SSH login. | Read the upload, verify it, and publish one directory. |

Acceptance refuses to run as root. On Linux it also refuses to run as the user that owns the upload directory. Windows does not have those user ids. Windows also cannot publish: the command stops before publication on any host that is not Linux.

Do not create these users on a purchased server. No server is purchased. The accounts below are the shape a later Linux host must use.

## Files the uploader writes

The upload directory name is the release id in canonical lowercase UUID form. `11111111-1111-4111-8111-111111111111` is accepted. Uppercase, braces, and any name containing `..` or a slash are refused.

Four names, and no others:

```text
<release-id>.dump.partial
<release-id>.json.partial
<release-id>.json.sig.partial
COMMIT.partial
```

`COMMIT.partial` is written last. It is a completion marker. It records the size and SHA-256 of the other three files so acceptance can see that the copy matches the bytes the uploader finished writing. It is not a signature. The transfer login can write both the files and the marker, so a matching marker does not prove who published the snapshot.

`READY` is written by the acceptance login inside the private directory, and only after the checks pass. It is a publication marker. It is not authentication. A directory that does not contain `READY` is not an accepted release.

## What acceptance checks

Run this as `of_publish_accept` from the repository root:

```text
python -m ops.accept_transfer accept <release-id> --incoming /var/openfactors/transfer/incoming --accept-root /var/openfactors/accept --allowed-signers /var/openfactors/pin/allowed_signers
```

The command takes one non-blocking exclusive lock for that release id. If another acceptance holds the lock, this one stops and leaves every directory as it is.

It then:

1. Opens the upload directory with `O_DIRECTORY` and `O_NOFOLLOW` on Linux and keeps that descriptor for the copy. A symlink directory is refused. Replacing the path after the open does not change which directory is read, because later names are opened relative to the descriptor. A file symlink, an unexpected name, or a file over the size guard is refused.
2. Copies each file into a private staging directory. The copy is a new file, including when the upload name is a hard link. A hard link is a second name for a regular file. Acceptance does not publish that inode. Another name can rewrite the bytes in place; the second read and the SHA-256 in `COMMIT.partial` catch that. A changed inode, a changed size, or a second read that does not match the first read discards staging.
3. Verifies the staged manifest with `ssh-keygen -Y verify` against the pinned allowed-signers file. The principal is `openfactors-publisher`. The namespace is `openfactors-snapshot`. The upload cannot choose that file or those names.
4. Reads the manifest only after the signature matches. The manifest `release_id` must be the directory name. The dump SHA-256 in that manifest must match the staged dump.
5. Writes `READY` and publishes the staging directory with Linux `renameat2(RENAME_NOREPLACE)`. The final name appears only when the dump, the manifest, the signature, and `READY` are already inside it.

An existing `accepted/<release-id>` is not replaced. The same bytes are a success. Different bytes are an error, and the existing directory stays. This includes a second upload whose marker and signature are internally consistent but not the same bytes.

These limits are provisional rejection limits, not a measured disk capacity. The dev custom dump on 2026-10-27 was 3,523,158 bytes.

| File | Maximum bytes |
|---|---|
| Dump | 32,000,000 |
| Manifest | 65,536 |
| Signature | 16,384 |
| `COMMIT.partial` | 4,096 |

## Filesystem permissions

The transfer chroot and the acceptance directory are on the same filesystem, so the final rename is one directory operation. They are not the same directory. The allowed-signers file is not inside the chroot.

| Path | Owner and group | Mode | Who may use it |
|---|---|---|---|
| `/var/openfactors/transfer` | `root:root` | `0755` | Chroot. Not writable. |
| `transfer/incoming` | `of_publish_transfer:of_publish_accept` | `2750` | Uploader writes. Accept user reads. setgid keeps the group. |
| `incoming/<uuid>` | `of_publish_transfer:of_publish_accept` | `2750` | One release. The four partial names only. |
| `*.partial` | `of_publish_transfer:of_publish_accept` | `0640` | Uploader writes them before the marker. Accept user reads them. |
| `/var/openfactors` | `root:root` | `0755` | Not writable by either login. |
| `/var/openfactors/pin` | `root:of_publish_accept` | `0750` | Root owns it. The accept user can traverse and read. Neither login can create or unlink here. |
| `pin/allowed_signers` | `root:of_publish_accept` | `0440` | The pin. It is not inside `accept/`, `incoming/`, or the chroot. |
| `/var/openfactors/accept` | `of_publish_accept:of_publish_accept` | `0750` | Outside the chroot. Writable by the accept user, so the pin must not live here. |
| `accept/staging` | `of_publish_accept:of_publish_accept` | `0700` | Private copies. Discarded after publish or refusal. |
| `accept/locks` | `of_publish_accept:of_publish_accept` | `0700` | One lock file per release id. |
| `accept/accepted` | `of_publish_accept:of_publish_accept` | `0750` | Parent stays writable by the accept user so a new release can be renamed in. |
| `accept/accepted/<uuid>` | `of_publish_accept:of_publish_accept` | `0550` | Published once. Files are `0440`. The directory stays owner-writable until `renameat2` returns, because Linux must update `..`, and is then sealed to `0550`. |

A mode of `0700` on the upload would stop the accept user from reading it. Group read is what makes the copy possible without letting every account on the machine read the upload. The transfer user is not a member of `of_publish_accept`, so that user cannot read `accepted/`.

The SFTP user's umask is `0027`, so new files are `0640`. A later host can use this SSH match. It is not installed here.

```text
Match User of_publish_transfer
    ChrootDirectory /var/openfactors/transfer
    ForceCommand internal-sftp -d /incoming
    AllowTcpForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitTTY no
```

The pin is outside every directory `of_publish_accept` or `of_publish_transfer` can write. On POSIX the command walks `lstat` from the pin file to the filesystem root. That walk is the trusted boundary. It refuses a symlink in the chain, and it refuses a path that contains `..`, so lexical cleanup cannot skip that symlink. It refuses the file when the acceptance user can write it. It refuses a parent when that user can unlink or rename the next component, even if the pin file itself is mode `0440`. The directory owner can do that whenever the directory is writable. The sticky bit is the exception: a sticky directory does not let this user unlink a child owned by someone else. Windows does not expose those owner bits, so the walk is not used there. Windows still cannot publish.

The command sets the mode bits it creates under the accept root. It does not create users, does not change SSH, and does not change group ownership. On the later host, root creates the pin directory and its file. Do not put the pin under `/var/openfactors/accept`. Set the group before the first run:

```text
chgrp of_publish_accept /var/openfactors/accept /var/openfactors/accept/accepted /var/openfactors/pin
```

Do not run that command on this development computer. There is no serving host yet.

## Recovery

If the command stops before the rename, `accepted/<release-id>` does not exist. Staging may remain. The next acceptance for that same release deletes only `accept/staging/<release-id>.*`. It does not delete `accepted/`.

If the command reports an error, look at `accepted/<release-id>/READY` before removing anything. A directory with `READY` may already be the published release. Retry the same upload. Identical bytes succeed and do not rewrite the directory.

A directory without `READY` is not an accepted release. The command will not delete it and will not publish over it. Move that directory aside by hand after you confirm the path, then run acceptance again. Do not point a restore at it.

Different bytes for an existing release are left in place. Do not delete that directory from this command. Inspect it later with the signature check in `snapshot-signature.md`. Serving restore reads this directory with `restore-accepted staging <release-id>`. Root creates the private parent `/var/openfactors/restore` once, as mode `0700`. The command then creates its own child `/var/openfactors/restore/<release-id>.<pid>.<token>` and copies the files there. The command deletes only that one child. After a crash, root may remove that same child after confirming no restore process is running. Do not delete the parent directory, and do not delete `accepted/`.

The private parent is not created by the command. Root creates it once:

```text
install -d -o root -g root -m 0700 /var/openfactors/restore
```

Do not run that command on this development computer. There is no serving host yet.

If the error says another acceptance is running, wait for that process. Do not delete the lock by hand while it is running.

Publication itself is Linux `renameat2(RENAME_NOREPLACE)`. The manifest signature command still has a Windows branch that uses `os.rename`. Do not use that branch to publish an accepted release. A Windows development machine can run the checks in tests, and the real publication primitive stays the Linux one.

## Threat assumptions

| Threat | What acceptance does |
|---|---|
| The transfer user reads the signing key or the pin | The private key is not on this host. The pin is outside the chroot. |
| A symlink or `..` in the upload | Linux opens the directory with `O_DIRECTORY` and `O_NOFOLLOW`, then opens names from that descriptor. A symlink is also refused by the path check on a host that cannot hold that descriptor. |
| The upload path is replaced after the open | Linux keeps reading the original directory descriptor. A host without that open refuses the changed inode. |
| A hard link in the upload | The bytes are copied into a new file. The upload inode is not what gets published. |
| The transfer user publishes a directory by renaming it | Only the accept user publishes, and only after the signature and the dump hash match. |
| The manifest and the dump are replaced together | The signature is over the manifest bytes and fails. |
| The dump changes under a manifest that still verifies | The dump SHA-256 inside the signed manifest fails. |
| The same inode and size are rewritten in place | The copied SHA-256 does not match `COMMIT.partial`, or the second read does not match the first. |
| A second upload uses different bytes | The existing accepted directory stays. |
| The same upload is retried | The byte match is a success and does not rewrite the directory. |
| The process is killed before the rename | `accepted/<release-id>` does not exist. The next run discards staging. |
| Acceptance opens PostgreSQL | This command has no database call. |

`COMMIT.partial` does not authenticate the publisher. `READY` does not authenticate the publisher. The Ed25519 signature and the pinned allowed-signers file do.

The accept user can still change modes on its own tree. The transfer user cannot. `restore-accepted` reads `accepted/<release-id>` through a directory descriptor, requires `READY`, refuses a symlink, verifies the manifest signature against `/var/openfactors/pin/allowed_signers`, and verifies the dump SHA-256 before `CREATE DATABASE`. Promotion on `staging` then requires the comment `openfactors-authenticated`. That promotion trusts the stored comment. It does not read the signature again. Rollback and cleanup do not read the accepted directory.

The pin file is the trust root. It is chosen by the operator on the command line, from `/var/openfactors/pin/allowed_signers`. It is not taken from the upload, and it is not stored under a directory the acceptance user can write. The transfer public key must not be a line in that file. The principal stays `openfactors-publisher` and the namespace stays `openfactors-snapshot`. Mode checks on the pin and the private restore directory do not see ACLs. A default ACL or an access ACL can grant write that the mode bits hide. Root can replace the pin or write the catalog comment directly.

Accepted files are one disk. They are not a backup of `openfactors_dev`, and they are not an offsite copy.

ADR-003 sets a USD 10 recurring ceiling. Current recurring spend remains USD 0. No VPS is purchased. The conditional acceptance of the trust limits on this page is ADR-010. Production verification of those limits is still open. Public Beta v0.1 remains no-go.
