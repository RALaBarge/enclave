# Building the DeskBox binaries

`provision.sh` installs prebuilt static binaries from `image/artifacts/` (not committed). Build them on any machine with
Go 1.26+ from the commit pinned as `DESKBOX_COMMIT` in `manifest.env`:

```sh
git clone https://github.com/ralabarge/deskbox && cd deskbox && git checkout <DESKBOX_COMMIT>
for b in agent-desk tcs-shim tcs-verify; do
  CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o <repo>/image/artifacts/$b ./cmd/$b
done
```

Note: DeskBox is PolyForm Noncommercial 1.0.0; check that licence before handing the image to anyone.
