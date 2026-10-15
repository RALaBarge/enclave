# Building the DeskBox binaries

`provision.sh` installs prebuilt static binaries from `image/artifacts/` (not committed). Build them on any machine with
Go 1.26+ from the commit pinned as `DESKBOX_COMMIT` in `manifest.env`:

```sh
git clone https://github.com/ralabarge/deskbox && cd deskbox && git checkout <DESKBOX_COMMIT>
for b in agent-desk tcs-shim tcs-verify; do
  CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o <repo>/image/artifacts/$b ./cmd/$b
done
```

Licence: this repo (deskbox-sbx) is Apache-2.0. The DeskBox binaries baked into the image carry the licence of the
`ralabarge/deskbox` repo at the pinned commit, which was PolyForm Noncommercial 1.0.0 when the image was built. Relicensing
that repo does not change commits already released; rebuild from a commit that carries the new licence before handing the image out.
