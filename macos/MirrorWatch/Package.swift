// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "MirrorWatch",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "MirrorWatch", targets: ["MirrorWatch"])
    ],
    targets: [
        .executableTarget(
            name: "MirrorWatch",
            path: "Sources/MirrorWatch"
        )
    ]
)
