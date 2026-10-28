# Snapshot manifest signature

Public Beta v0.1 stays no-go. No host is purchased. This command signs an existing snapshot manifest. It does not transfer the snapshot, create a database, or change `openfactors_dev`.

The manifest schema is unchanged. The dump SHA-256 check is unchanged. Local dev and test restore do not require a signature. Staging restore does: `restore-accepted` verifies this signature before it creates a candidate. Rollback and cleanup do not read the signature.

## Keys

Two keys stay separate. This command uses only the publisher signing key.

| Key | File | Use |
|---|---|---|
| Publisher signing key | `ops/local/runtime/dev/snapshot-publisher` | `ssh-keygen -Y sign` only |
| Transfer key | `ops/local/runtime/dev/snapshot-transfer` | The SFTP login in `snapshot-transfer-acceptance.md`. Not created by this command. |

Acceptance of an SFTP upload is described in `snapshot-transfer-acceptance.md`. This signature command still does not create that login. The private signing key stays on the development computer, under `ops/local/runtime/`, which Git ignores. It is an unencrypted Ed25519 file. On a POSIX host it must be mode `0600`; a key other accounts can read is refused. Windows hides those mode bits, so the account itself is the protection there. The command does not print the key. Do not commit it, do not pass the transfer key to `--key`, and do not copy the private key to the serving host. The serving host receives only the public allowed-signers line.

Anyone who can use the development account can read this unencrypted key and sign a manifest. A compromise of that account is a compromise of the publisher.

The public pin is an OpenSSH allowed-signers file on the machine that verifies. It is not a field in the manifest. The verifier always uses principal `openfactors-publisher` and namespace `openfactors-snapshot`. An uploaded manifest cannot change those names or the path of the pin.

Create the signing key once, from the repository root:

```text
ssh-keygen -q -t ed25519 -f ops/local/runtime/dev/snapshot-publisher -N "" -C openfactors-publisher
```

Put the public line in the allowed-signers file. The principal is the first field:

```text
openfactors-publisher namespaces="openfactors-snapshot" ssh-ed25519 AAAA... openfactors-publisher
```

Do not put the transfer public key in that file.

## Commands

Sign the manifest that `create` already wrote. The signature is the sibling `<manifest>.sig`, which for a release manifest is `<release-id>.json.sig`. Signing writes a private temporary file beside it and publishes that complete file only when the final name is absent. An existing signature is left in place. A failed attempt removes the temporary file and does not leave a final `.sig`. A name ending in `.partial` is not a signature.

```text
python -m ops.publish_snapshot sign-manifest ops/local/runtime/dev/publications/<release-id>.json --key ops/local/runtime/dev/snapshot-publisher
```

Verify those exact manifest bytes. The dump checksum is still a separate check.

```text
python -m ops.publish_snapshot verify-manifest ops/local/runtime/dev/publications/<release-id>.json --allowed-signers ops/local/runtime/dev/allowed_signers
```

`--signature` selects a signature path. When it is omitted, the sibling `.sig` file is used. Verification passes the manifest bytes to `ssh-keygen -Y verify` on standard input. A missing signature, a changed manifest, a different key, a different principal or namespace, an invalid or unreadable allowed-signers file, a malformed signature, or a failed `ssh-keygen` process is rejected.

## Rotation

Generate a new publisher key beside the old one. Add its public line to the allowed-signers file, then sign new manifests with the new key. Keep the old public line until older signatures no longer need to verify. Then remove the old line. Snapshots already signed by the removed key stop verifying. The transfer key, when it exists later, rotates by replacing its SSH login line. It is not a substitute for the publisher key.

Public Beta v0.1 remains no-go.
