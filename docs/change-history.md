# Change history

Schema `1.0.0` is the only public schema. It has twelve fields in a fixed order. No public field is deprecated. A breaking change is a new `dataset_version`. A breaking change is a removed field, a changed meaning, a changed type, a changed order, or a changed nullability rule. `dataset_version` is on every row and in the dictionary.

A change to values is a new `release_id`. The old release stays readable when you pin it, unless it has been withdrawn. An unknown pin and a withdrawn pin both return `That release is not available.` A corrected source value is a new release. It does not edit the old one.

To reproduce an analysis, save the `release_id` from the first response body and from `X-Release-Id`, and save `dataset_version` from a row. Send that same `release_id` on later pages and on the export. The active release can move. The pin does not.

No schema has been replaced. This note does not list releases. The same section is on the API guide at `/docs`.
