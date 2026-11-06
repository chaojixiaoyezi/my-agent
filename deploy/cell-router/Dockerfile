FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/cell-router ./cmd/cell-router

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/cell-router /cell-router
USER 10001
EXPOSE 8080 8081
ENTRYPOINT ["/cell-router"]
