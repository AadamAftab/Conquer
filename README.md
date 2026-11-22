# ApexBastion

## Run

Build the image:

```bash
docker build -t apex-bastion .
```

Check it under the match limits:

```bash
python check/check.py --map-dir /arena -- docker run --rm -i \
  --network none --cpus 1 --memory 512m --read-only \
  --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  -v "${ARENA_DIR}:/arena:ro" apex-bastion
```
