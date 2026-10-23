# Building the DeskBox binaries

`provision.sh` installs prebuilt static binaries from `image/artifacts/` (not committed). Build them on any machine with
Go 1.26+ from the commit pinned as `DESKBOX_COMMIT` in `manifest.env`:

```sh
git clone https://github.com/ralabarge/deskbox && cd deskbox && git checkout <DESKBOX_COMMIT>
for b in agent-desk tcs-shim tcs-verify; do
  CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o <repo>/image/artifacts/$b ./cmd/$b
done
```

`DESKBOX_COMMIT` is on `origin/sandbox-bind`; the clone-and-checkout above is the clean build path.

Licence: this repo and DeskBox are both MIT. The binaries are built from the pinned `DESKBOX_COMMIT` (`e6fd82f` was the
commit that relicensed DeskBox to MIT, identical to the earlier `7526699`; the current pin, `79f4c0e`, adds `sandbox.bind` and `-job-key`).
