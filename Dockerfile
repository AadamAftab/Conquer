FROM golang:1.26.1-bookworm@sha256:ab3d6955bbc813a0f3fdf220c1d817dd89c0b3f283777db8ece4a32fe7858edd AS build
WORKDIR /src
ENV CGO_ENABLED=0 GOOS=linux GOARCH=amd64
COPY go.mod ./
COPY citadel ./citadel
COPY vanguard ./vanguard
COPY cmd/citadel ./cmd/citadel
RUN go build -trimpath -ldflags="-s -w" -o /aadam-citadel ./cmd/citadel

FROM scratch
ENV HOME=/tmp TMPDIR=/tmp GOMAXPROCS=1
COPY --from=build /aadam-citadel /aadam-citadel
ENTRYPOINT ["/aadam-citadel"]
