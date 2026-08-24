module github.com/rat-data/rat/sdk-go

go 1.26.0

require (
	connectrpc.com/connect v1.20.0
	github.com/rat-data/rat/platform v0.0.0
	golang.org/x/net v0.58.0
)

require (
	golang.org/x/text v0.41.0 // indirect
	google.golang.org/protobuf v1.36.12 // indirect
)

replace github.com/rat-data/rat/platform => ../platform
